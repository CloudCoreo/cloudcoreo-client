#!/usr/bin/env python
######################################################################
# Cloudcoreo client
#   example of a debug run:
#       workdir=/tmp/53d6375b01f98c8230f72e83 ;set | grep -v -e " " -e IFS -e SHELLOPTS -e rvm -e RVM >
# "$workdir/env.out"; rm -rf $workdir/5* ;python cloudcoreo-client.py --debug --access-key-id <your access key id>
# --secret-access-key <your secret access key>
# --queue-url https://sqs.us-east-1.amazonaws.com/910887748405/coreo-asi-<asi-id>-i-db404ff1 --work-dir $workdir
# --cloudcoreo-secret-key "<cloudcoreo secret key>" --cloudcoreo-url http://localhost:3000
# --asi-id "<asi-id>" --server-name server-nat
#
######################################################################
import time
import boto3
import datetime
import json
import logging
import string
import subprocess
import traceback
import unicodedata
from optparse import OptionParser
from tempfile import mkstemp

import boto.sqs
import os
import re
import requests
import stat
import sys
import yaml

SQS_GET_MESSAGES_SLEEP_TIME = 10
SQS_VISIBILITY_TIMEOUT = 0
logging.basicConfig()


def get_sqs_messages(queue_url):
    client = boto3.client('sqs')
    while 1:
        response = client.receive_message(
            QueueUrl=queue_url,
            VisibilityTimeout=SQS_VISIBILITY_TIMEOUT,
            WaitTimeSeconds=20
        )
        print response
        time.sleep(SQS_GET_MESSAGES_SLEEP_TIME)


def parse_args():
    parser = OptionParser("usage: %prog [options]")
    parser.add_option("--access-key-id", dest="accessKeyId", default=None,
                      help="The access key id for the cloudcoreo IAM user")
    parser.add_option("--secret-access-key", dest="secretAccessKey", default=None,
                      help="The secred access key for the cloudcoreo IAM user")
    parser.add_option("--queue-url", dest="queueUrl", default=None,
                      help="The url for the sqs queue owned by cloudcoreo")
    parser.add_option("--work-dir", dest="workDir", default=None,
                      help="Where cloudcoroe should be doing the work (git clone etc)")
    parser.add_option("--cloudcoreo-secret-key", dest="ccSecretKey", default=None, help="Cloudcoreo authentication key")
    parser.add_option("--cloudcoreo-url", dest="ccUrl", default=None, help="CloudCoreo API endpoint")
    parser.add_option("--asi-id", dest="asiId", default=None, help="The appstack instance ID this server belongs to")
    parser.add_option("--server-name", dest="serverName", default=None, help="This servers name")
    parser.add_option("--debug", dest="debug", default=False, action="store_true",
                      help="Whether or not to run the app in debug mode [default: %default]")
    parser.add_option("--version", dest="version", default=False, action="store_true",
                      help="Display the current version")
    parser.add_option("--log-file", dest="logFile", default="/var/log/cloudcoreo-client.log",
                      help="The log file in which to dump debug information [default: %default]")
    return parser.parse_args()


def log(statement):
    statement = str(statement)
    if options.logFile is None:
        return
    if not os.path.exists(os.path.dirname(options.logFile)):
        os.makedirs(os.path.dirname(options.logFile))
    log_file = open(options.logFile, 'a')
    ts = datetime.datetime.now()
    is_first = True
    for line in statement.split("\n"):
        if is_first:
            if options.debug:
                print("%s - %s\n" % (ts, line))
            else:
                log_file.write("%s - %s\n" % (ts, line))
            is_first = False
        else:
            if options.debug:
                print("%s -    %s\n" % (ts, line))
            else:
                log_file.write("%s -    %s\n" % (ts, line))
    log_file.close()


def get_availability_zone():
    # cached
    global MY_AZ
    if MY_AZ is None:
        if options.debug:
            MY_AZ = 'us-east-1a'
        else:
            MY_AZ = meta_data("placement/availability-zone")
    return MY_AZ


def get_region():
    region = get_availability_zone()[:-1]
    log("region: %s" % region)
    return region


def meta_data(data_path):
    # using 169.254.169.254 instead of 'instance-data' because some people
    # like to modify their dhcp tables...
    return requests.get('http://169.254.169.254/latest/meta-data/%s' % data_path).text


def get_coreo_key():
    content = open("%s/git_key.out" % options.workDir, 'r').read()
    return json.loads(content)


def get_coreo_appstack():
    content = open("%s/appstack.out" % options.workDir, 'r').read()
    return json.loads(content)


def get_coreo_appstackinstance_config():
    content = open("%s/appstack_instance_config.out" % options.workDir, 'r').read()
    return json.loads(content)


def get_coreo_appstackinstance():
    content = open("%s/appstack_instance.out" % options.workDir, 'r').read()
    return json.loads(content)


def mkdir_p(path):
    if not os.path.exists(path):
        log("creating path [%s]" % path)
        os.makedirs(path)
        log("created path [%s]" % path)


def clone_for_asi(branch, revision, repo_url, key_material, work_dir):
    mkdir_p(work_dir)
    fd, temp_key_path = mkstemp()

    # lets write the private key material to a temp file
    # this creates a file in the most secure way possible
    # so no chmod is necessary
    pkey = open(temp_key_path, 'w')
    log("writing private key material to %s" % temp_key_path)
    pkey.write(key_material)
    pkey.close()
    os.close(fd)

    # now we need to have the public key there as well
    # p = subprocess.Popen(['ssh-keygen', '-y', '-f', temp_key_path, '>', "%s.pub" % temp_key_path],
    #                      stdin=subprocess.PIPE,
    #                      stdout=subprocess.PIPE,
    #                      stderr=subprocess.PIPE,
    #                      preexec_fn=os.setsid
    #                      )

    # now we set up the git-ssh wrapper script
    fd_ssh, ssh_wrapper_path = mkstemp()
    log("writing ssh wrapper script to %s" % ssh_wrapper_path)
    wrap = open(ssh_wrapper_path, 'w')
    wrap.write("""#!/bin/sh
/usr/bin/ssh -o StrictHostKeyChecking=no -i "%s" "$@" 
    """ % temp_key_path)
    wrap.close()
    os.close(fd_ssh)
    st = os.stat(ssh_wrapper_path)
    os.chmod(ssh_wrapper_path, st.st_mode | stat.S_IEXEC)

    # now we do the cloning
    log("os.chdir(%s)" % work_dir)
    os.chdir(work_dir)
    log("cloning repo from url: %s" % repo_url.strip())
    git(ssh_wrapper_path, work_dir, "clone", repo_url.strip(), "repo")
    log("os.chdir(%s/repo)" % work_dir)
    os.chdir("%s/repo" % work_dir)

    if branch is not None:
        log("checking out branch %s" % branch)
        git(ssh_wrapper_path, "%s/repo" % work_dir, "checkout", branch)

    if revision is not None:
        log("checking out revision %s" % revision)
        git(ssh_wrapper_path, "%s/repo" % work_dir, "checkout", revision)

    log("completed recursive checkout")
    git(ssh_wrapper_path, "%s/repo" % work_dir, "submodule", "update", "--recursive", "--init")
    log("completed recursive checkout")


def get_config():
    config = get_coreo_appstackinstance_config()
    log("got config: %s" % config)
    return config


def get_default_config():
    config = get_coreo_appstack()
    log("got appstack: %s" % config)
    return config


def git(ssh_wrapper, git_dir, *args):
    log("setting environment GIT_SSH=%s" % ssh_wrapper)
    os.environ['GIT_SSH'] = "%s" % ssh_wrapper
    log("cwd=%s" % git_dir)
    log("running command: %s" % str(['git'] + list(args)))
    p = subprocess.Popen(['git'] + list(args),
                         cwd=git_dir,
                         stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE,
                         preexec_fn=os.setsid,
                         shell=False
                         )
    (std_out_data, std_err_data) = p.communicate()
    log("(std_out_data, std_err_data) = (%s, %s)" % (std_out_data, std_err_data))
    return p


def get_environment_dict():
    environment = {}
    default_config = get_default_config()
    log("got default from appstack [%s]" % default_config)
    config = get_config()
    log("got config [%s]" % config)
    default_vars = json.loads(
        re.sub('\n', r'', unicodedata.normalize('NFKD', default_config['config']).encode('ascii', 'ignore')))
    instance_vars = json.loads(
        re.sub('\n', r'', unicodedata.normalize('NFKD', config['document']).encode('ascii', 'ignore')))
    all_vars = {'variables': default_vars['variables']}
    all_vars['variables'].update(instance_vars['variables'])
    for var in all_vars['variables']:
        value = all_vars['variables'][var]
        log("value: %s" % value)
        # if ! value['value'].nil? then
        if 'value' in value.keys() and value['value'] is not None:
            # environment[var.to_s] = value['value']
            environment[var] = value['value']
        else:
            # elsif ! value['default'].nil? then
            environment[var] = value.get('default', '')
    return environment


######################################################################
# this is all to deal with overriding service config.rb files
######################################################################
def get_script_order_files(root_dir, server_name):
    order_files = []
    log("walking rootDir: %s" % root_dir)
    for dir_name, subdir_list, file_list in os.walk(root_dir):
        if ".git" in dir_name:
            continue
        for file_name in file_list:
            full_path = "%s/%s" % (dir_name, file_name)
            log("checking file [%s]" % full_path)
            log("checking if server_name [%s] is in full_path" % (server_name.lower()))
            if server_name.lower() not in full_path.lower():
                continue
            strings_replaced = string.replace(
                string.replace(string.replace(full_path.lower(), "extends", ""), "#{server_name.downcase}", ""), "/",
                "")
            log("checking if boot-scritsorder is in [%s]" % strings_replaced)
            if "boot-scriptsorder" in strings_replaced:
                order_files.append(full_path)
    if len(order_files) == 0 and server_name != "repo":
        order_files = get_script_order_files(root_dir, "repo")
    log("found ordered_files: %s" % order_files)
    order_files.sort(key=len, reverse=True)
    log("order_files %s" % order_files)
    return order_files


def set_env(env_list):
    for (key, val) in env_list.items():
        log("os.environ[%s] = %s" % (key, str(val)))
        os.environ[key] = str(val).strip().strip('"')

    # the order matters here - the env.out has to be last
    with open("%s/env.out" % options.workDir) as f:
        for line in f:
            values = line.split('=')
            if len(values) == 2:
                log("os.environ[%s] = %s" % (values[0], values[1]))
                os.environ[values[0]] = str(values[1]).strip().strip('"')


def run_cmd(work_dir, *args):
    print("cwd=%s" % work_dir)
    print("running command: %s" % str(list(args)))
    with open(options.logFile, 'a') as log_file:
        proc_ret_code = subprocess.call(list(args),
                                        cwd=work_dir,
                                        shell=False,
                                        stdout=log_file,
                                        stderr=log_file)

    if proc_ret_code == 0:
        ## return the return code
        log("Success running script [%s]" % list(args))
        log("  returning rc [%d]" % proc_ret_code)
        return None
    else:
        return proc_ret_code


def run_all_boot_scripts(repo_dir, server_name_dir):
    env = {}
    env = get_environment_dict()
    script_order_files = []
    script_order_files = get_script_order_files(repo_dir, server_name_dir)
    log("setting env [%s]" % env)

    full_run_error = None
    for f in script_order_files:
        log("loading file [%s]" % f)
        my_doc = yaml.load(open(f, "r"))
        log("got yaml doc [%s]" % my_doc)
        if my_doc is None or my_doc['script-order'] is None:
            continue
        log("[%s]" % my_doc['script-order'])
        # order = YAML.load_file(f)
        # order['script-order'].each { |script|
        for script in my_doc['script-order']:
            # full_path = File.join(File.dirname(f), script)
            full_path = os.path.join(os.path.dirname(f), script)
            log("running script [%s]" % full_path)
            os.chmod(full_path, stat.S_IEXEC)
            set_env(env)
            if full_path in open(LOCK_FILE_PATH, 'r').read():
                log("skipping run of [%s]. Already run" % script)
                continue
            # we need to check the error and output if we are debugging or not
            err = None
            out = None
            if not options.debug:
                err = run_cmd(os.path.dirname(full_path), "./%s" % os.path.basename(full_path))
            if not err:
                with open(LOCK_FILE_PATH, 'a') as lockFile:
                    lockFile.write("%s\n" % full_path)
            else:
                full_run_error = err
                log(err)
            log(out)

    # if we have not received any errors for the whole run, lets mark the bootstrap lock as complete
    if not full_run_error:
        with open(LOCK_FILE_PATH, 'a') as lockFile:
            lockFile.write(COMPLETE_STRING)


def bootstrap():
    #  asi = get_coreo_appstackinstance_response("")
    log("getting response from server")
    asi = get_coreo_appstackinstance()
    appstack = get_coreo_appstack()
    key = get_coreo_key()
    #  Coreo::GIT.clone_for_asi("#{DaemonKit.arguments.options[:asi_id]}", asi['branch'], asi['revision'],
    # asi['gitUrl'], asi['keyMaterial'], "#{DaemonKit.arguments.options[:work_dir]}")
    clone_for_asi(asi['branch'], asi['revision'], appstack['gitUrl'], key['keyMaterial'], options.workDir)
    #  run_all_boot_scripts("#{DaemonKit.arguments.options[:work_dir]}", "#{DaemonKit.arguments.options[:server_name]}")
    run_all_boot_scripts(options.workDir, options.serverName)


(options, args) = parse_args()

# globals for caching
MY_AZ = None
version = '0.1.14'
COMPLETE_STRING = "COREO::BOOTSTRAP::complete"

# lets set up a lock file so we don't rerun on bootstrap... this will
# also allow people to remove the lock file to rerun everything
LOCK_FILE_PATH = "%s/bootstrap.lock" % options.workDir

if options.version:
    print "%s" % version
    sys.exit(0)

while True:
    try:
        if not os.path.isfile(LOCK_FILE_PATH):
            # touch the bootstrap lock file to indicate we have started to run through it
            with open(LOCK_FILE_PATH, 'a'):
                os.utime(LOCK_FILE_PATH, None)
        if COMPLETE_STRING not in open(LOCK_FILE_PATH, 'r').read():
            bootstrap()
        SQS = boto.sqs.connect_to_region(get_region(),
                                         aws_access_key_id=options.accessKeyId,
                                         aws_secret_access_key=options.secretAccessKey)
        QUEUE_NAME = options.queueUrl.split('/')[-1].strip()
        QUEUE = SQS.get_queue(queue_name=QUEUE_NAME)

        if not QUEUE:
            raise ValueError, "Queue does not exist."

        messages = QUEUE.get_messages(1, wait_time_seconds=20)
        if len(messages):
            SQS = None
            os.environ['AWS_ACCESS_KEY_ID'] = ''
            os.environ['AWS_SECRED_ACCESS_KEY'] = ''
            raw_message = messages[0]
            message = json.loads(raw_message.get_body())
            message_type = message['type']
            if message_type.lower() == 'runcommand':
                try:
                    script = message['payload']
                    if not options.debug:
                        os.chmod(script, stat.S_IEXEC)
                        os.system(script)
                except Exception as ex:
                    log("exception: %s" % str(ex))
            else:
                log("unknown message type")
            QUEUE.delete_message(message)
        SQS = None
    except Exception as ex:
        log("Exception caught: [%s]" % str(ex))
        log(traceback.format_exc())
        if options.debug:
            sys.exit(1)

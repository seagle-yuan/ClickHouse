#!/usr/bin/env python3

import logging
import subprocess
import os
import json
import csv
from github import Github
from pr_info import PRInfo
from s3_helper import S3Helper
from get_robot_token import get_best_robot_token
from upload_result_helper import upload_results
from docker_pull_helper import get_image_with_version
from commit_status_helper import post_commit_status

NAME = 'Fast test (actions)'

def get_fasttest_cmd(workspace, output_path, ccache_path, repo_path, pr_number, commit_sha, image):
    return f"docker run --cap-add=SYS_PTRACE " \
        f"-e FASTTEST_WORKSPACE=/fasttest-workspace -e FASTTEST_OUTPUT=/test_output " \
        f"-e FASTTEST_SOURCE=/ClickHouse --cap-add=SYS_PTRACE " \
        f"-e PULL_REQUEST_NUMBER={pr_number} -e COMMIT_SHA={commit_sha} -e COPY_CLICKHOUSE_BINARY_TO_OUTPUT=1 " \
        f"--volume={workspace}:/fasttest-workspace --volume={repo_path}:/ClickHouse --volume={output_path}:/test_output "\
        f"--volume={ccache_path}:/fasttest-workspace/ccache {image}"


def process_results(result_folder):
    test_results = []
    additional_files = []
    # Just upload all files from result_folder.
    # If task provides processed results, then it's responsible for content of result_folder.
    if os.path.exists(result_folder):
        test_files = [f for f in os.listdir(result_folder) if os.path.isfile(os.path.join(result_folder, f))]
        additional_files = [os.path.join(result_folder, f) for f in test_files]

    status_path = os.path.join(result_folder, "check_status.tsv")
    logging.info("Found test_results.tsv")
    status = list(csv.reader(open(status_path, 'r'), delimiter='\t'))
    if len(status) != 1 or len(status[0]) != 2:
        return "error", "Invalid check_status.tsv", test_results, additional_files
    state, description = status[0][0], status[0][1]

    results_path = os.path.join(result_folder, "test_results.tsv")
    test_results = list(csv.reader(open(results_path, 'r'), delimiter='\t'))
    if len(test_results) == 0:
        raise Exception("Empty results")

    return state, description, test_results, additional_files



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    temp_path = os.getenv("TEMP_PATH", os.path.abspath("."))
    caches_path = os.getenv("CACHES_PATH", temp_path)

    if not os.path.exists(temp_path):
        os.makedirs(temp_path)

    with open(os.getenv('GITHUB_EVENT_PATH'), 'r') as event_file:
        event = json.load(event_file)

    pr_info = PRInfo(event)

    gh = Github(get_best_robot_token())

    docker_image = get_image_with_version(temp_path, 'clickhouse/fasttest')

    s3_helper = S3Helper('https://s3.amazonaws.com')

    workspace = os.path.join(temp_path, "fasttest-workspace")
    if not os.path.exists(workspace):
        os.makedirs(workspace)

    output_path = os.path.join(temp_path, "fasttest-output")
    if not os.path.exists(output_path):
        os.makedirs(output_path)

    cache_path = os.path.join(caches_path, "fasttest")
    if not os.path.exists(cache_path):
        os.makedirs(cache_path)

    repo_path = os.path.join(temp_path, "fasttest-repo")
    if not os.path.exists(repo_path):
        os.makedirs(repo_path)

    run_cmd = get_fasttest_cmd(workspace, output_path, cache_path, repo_path, pr_info.number, pr_info.sha, docker_image)
    logging.info("Going to run fasttest with cmd %s", run_cmd)

    logs_path = os.path.join(temp_path, "fasttest-logs")
    if not os.path.exists(logs_path):
        os.makedirs(logs_path)

    run_log_path = os.path.join(logs_path, 'runlog.log')
    with open(run_log_path, 'w') as log:
        retcode = subprocess.Popen(run_cmd, shell=True, stderr=log, stdout=log).wait()
        if retcode == 0:
            logging.info("Run successfully")
        else:
            logging.info("Run failed")

    subprocess.check_call(f"sudo chown -R ubuntu:ubuntu {temp_path}", shell=True)
    subprocess.check_call(f"sudo chown -R ubuntu:ubuntu {cache_path}", shell=True)

    test_output_files = os.listdir(output_path)
    additional_logs = []
    for f in test_output_files:
        additional_logs.append(os.path.join(output_path, f))

    test_log_exists = 'test_log.txt' in test_output_files or 'test_result.txt' in test_output_files
    test_result_exists = 'test_results.tsv' in test_output_files
    test_results = []
    if 'submodule_log.txt' not in test_output_files:
        description = "Cannot clone repository"
        state = "failure"
    elif 'cmake_log.txt' not in test_output_files:
        description = "Cannot fetch submodules"
        state = "failure"
    elif 'build_log.txt' not in test_output_files:
        description = "Cannot finish cmake"
        state = "failure"
    elif 'install_log.txt' not in test_output_files:
        description = "Cannot build ClickHouse"
        state = "failure"
    elif not test_log_exists and not test_result_exists:
        description = "Cannot install or start ClickHouse"
        state = "failure"
    else:
        state, description, test_results, additional_logs = process_results(output_path)

    report_url = upload_results(s3_helper, pr_info.number, pr_info.sha, test_results, [run_log_path] + additional_logs, NAME, True)
    print("::notice ::Report url: {}".format(report_url))
    post_commit_status(gh, pr_info.sha, NAME, description, state, report_url)

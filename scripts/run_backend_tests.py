# Copyright 2014 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Script for running backend tests in parallel.

This should not be run directly. Instead, navigate to the oppia/ folder and
execute:

    python -m scripts.run_backend_tests

You can also append the following options to the above command:

    --verbose prints the output of the tests to the console.

    --test_target=core.controllers.editor_test runs only the tests in the
        core.controllers.editor_test module. (You can change
        "core.controllers.editor_test" to any valid module path.)

    --test_path=core/controllers runs all tests in test files in the
        core/controllers directory. (You can change "core/controllers" to any
        valid subdirectory path.)

    --test_shard=1 runs all tests in shard 1.

    --generate_coverage_report generates a coverage report as part of the final
        test output (but it makes the tests slower).

    --ignore_coverage only has an affect when --generate_coverage_report
        is specified. In that case, the tests will not fail just because
        code coverage is not 100%.

Note: If you've made some changes and tests are failing to run at all, this
might mean that you have introduced a circular dependency (e.g. module A
imports module B, which imports module C, which imports module A). This needs
to be fixed before the tests will run.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import multiprocessing
import os
import random
import re
import socket
import string
import subprocess
import sys
import threading
import time

from typing import Dict, Final, List, Optional, Tuple, cast

# Added imports
import time

from . import install_third_party_libs
from core import feconf, utils

# This installs third party libraries before importing other files or importing
# libraries that use the builtins python module (e.g. build, utils).
install_third_party_libs.main()

from . import common
from . import concurrent_task_utils
from . import servers

COVERAGE_EXCLUSION_LIST_PATH: Final = os.path.join(
    os.getcwd(), 'scripts', 'backend_tests_incomplete_coverage.txt'
)

TEST_RUNNER_PATH: Final = os.path.join(
    os.getcwd(), 'core', 'tests', 'gae_suite.py'
)

# Added code
TEST_RUNNER_PATH: Final = os.path.join(
    os.getcwd(), 'core', 'tests', 'gae_suite.py'
)
LOG_LINE_PREFIX: Final = 'LOG_INFO_TEST: '
SHARDS_SPEC_PATH: Final = os.path.join(
    os.getcwd(), 'scripts', 'backend_test_shards.json'
)
SHARDS_WIKI_LINK: Final = (
    'https://github.com/oppia/oppia/wiki/Writing-backend-tests#common-errors'
)
_LOAD_TESTS_DIR: Final = os.path.join(
    os.getcwd(), 'core', 'tests', 'load_tests'
)

_PARSER: Final = argparse.ArgumentParser(
    description="""
Run this script from the oppia root folder:
    python -m scripts.run_backend_tests
IMPORTANT: Only one of --test_path,  --test_target, and --test_shard
should be specified.
""")

_EXCLUSIVE_GROUP: Final = _PARSER.add_mutually_exclusive_group()
_EXCLUSIVE_GROUP.add_argument(
    '--test_target',
    help='optional dotted module name of the test(s) to run',
    type=str)
_EXCLUSIVE_GROUP.add_argument(
    '--test_path',
    help='optional subdirectory path containing the test(s) to run',
    type=str)
_EXCLUSIVE_GROUP.add_argument(
    '--test_shard',
    help='optional name of shard to run',
    type=str)
_PARSER.add_argument(
    '--generate_coverage_report',
    help='optional; if specified, generates a coverage report',
    action='store_true')
_PARSER.add_argument(
    '--ignore_coverage',
    help='optional; if specified, tests will not fail due to coverage',
    action='store_true')
_PARSER.add_argument(
    '--exclude_load_tests',
    help='optional; if specified, exclude load tests from being run',
    action='store_true')
_PARSER.add_argument(
    '-v',
    '--verbose',
    help='optional; if specified, display the output of the tests being run',
    action='store_true')


def run_shell_cmd(
    exe: List[str],
    stdout: int = subprocess.PIPE,
    stderr: int = subprocess.PIPE,
    env: Optional[Dict[str, str]] = None
) -> str:
    """Runs a shell command and captures the stdout and stderr output.

    If the cmd fails, raises Exception. Otherwise, returns a string containing
    the concatenation of the stdout and stderr logs.
    """
    p = subprocess.Popen(exe, stdout=stdout, stderr=stderr, env=env)
    last_stdout_bytes, last_stderr_bytes = p.communicate()
    # Standard and error output is in bytes, we need to decode them to be
    # compatible with rest of the code. Sometimes we get invalid bytes, in which
    # case we replace them with U+FFFD.
    last_stdout_str = last_stdout_bytes.decode('utf-8', 'replace')
    last_stderr_str = last_stderr_bytes.decode('utf-8', 'replace')
    last_stdout = last_stdout_str.split('\n')

    if LOG_LINE_PREFIX in last_stdout_str:
        concurrent_task_utils.log('')
        for line in last_stdout:
            if line.startswith(LOG_LINE_PREFIX):
                concurrent_task_utils.log(
                    'INFO: %s' % line[len(LOG_LINE_PREFIX):])
        concurrent_task_utils.log('')

    result = '%s%s' % (last_stdout_str, last_stderr_str)

    if p.returncode != 0:
        raise Exception('Error %s\n%s' % (p.returncode, result))

    return result


class TestingTaskSpec:
    """Executes a set of tests given a test class name."""

    def __init__(
        self,
        test_target: str,
        generate_coverage_report: bool
    ) -> None:
        self.test_target = test_target
        self.generate_coverage_report = generate_coverage_report
        self.start_time = 0
        self.end_time = 0

    def run(self) -> List[concurrent_task_utils.TaskResult]:
        """Runs all tests corresponding to the given test target."""
        self.start_time = time.time()  # Record start time
        env = os.environ.copy()
        test_target_flag = '--test_target=%s' % self.test_target
        if self.generate_coverage_report:
            exc_list = [
                sys.executable, '-m', 'coverage', 'run',
                '--branch', TEST_RUNNER_PATH, test_target_flag
            ]
            rand = ''.join(random.choices(string.ascii_lowercase, k=16))
            data_file = '.coverage.%s.%s.%s' % (
                socket.gethostname(), os.getpid(), rand)
            env['COVERAGE_FILE'] = data_file
            concurrent_task_utils.log('Coverage data for %s is in %s' % (
                self.test_target, data_file))
        else:
            exc_list = [sys.executable, TEST_RUNNER_PATH, test_target_flag]

        try:
            result = run_shell_cmd(exc_list, env=env)
        except Exception as e:
            result = str(e)

        self.end_time = time.time()  # Record end time

        elapsed_time = self.end_time - self.start_time

        return [concurrent_task_utils.TaskResult('', False, [], [result]), elapsed_time]


def main(args: Optional[List[str]] = None) -> None:
    """Run the tests."""
    args = _PARSER.parse_args(args=args)
    verbose = args.verbose

    # Fetch the test targets.
    all_test_targets = common.get_all_test_targets(
        exclude_load_tests=args.exclude_load_tests)

    # Determine the test targets to run.
    if args.test_shard:
        test_targets = common.get_sharded_tests(
            args.test_shard, all_test_targets)
    elif args.test_path:
        test_targets = common.get_test_targets_from_test_path(
            args.test_path, all_test_targets)
    elif args.test_target:
        test_targets = [args.test_target]
    else:
        test_targets = all_test_targets

    # Prepare tasks.
    tasks: List[TestingTaskSpec] = []
    for test_target in test_targets:
        tasks.append(
            TestingTaskSpec(
                test_target,
                args.generate_coverage_report
            )
        )

    # Execute tasks.
    if verbose:
        concurrent_task_utils.log('')
    task_results: List[Tuple[str, List[concurrent_task_utils.TaskResult], float]] = []
    with concurrent_task_utils.ParallelTaskExecutor(
        multiprocessing.cpu_count(), len(tasks)
    ) as executor:
        for i, task in enumerate(tasks):
            executor.put(i, task.run)

        for i, task in enumerate(tasks):
            _, result = executor.get(i)
            task_results.append((task.test_target, result[0], result[1]))

    # Check test results.
    if verbose:
        concurrent_task_utils.log('')
    for test_target, result, elapsed_time in task_results:
        if verbose:
            concurrent_task_utils.log('')
            concurrent_task_utils.log(test_target + ':')
            concurrent_task_utils.log(result[0])
        if 'Exception' in result[0]:
            concurrent_task_utils.log(
                'Tests for %s failed. Task output:\n%s' % (
                    test_target, result[0]), print_time=True)
        else:
            concurrent_task_utils.log(
                'Tests for %s passed. Task output:\n%s' % (
                    test_target, result[0]), print_time=True)

    # Sort tests based on execution time in descending order
    sorted_tests = sorted(
        [(test, elapsed_time) for test, _, elapsed_time in task_results],
        key=lambda x: x[1],
        reverse=True
    )

    # Print sorted tests with execution times
    concurrent_task_utils.log('')
    for test, elapsed_time in sorted_tests:
        concurrent_task_utils.log(
            f'Test: {test}, Execution Time: {elapsed_time} seconds')

    if verbose:
        concurrent_task_utils.log('')


if __name__ == '__main__':
    main()

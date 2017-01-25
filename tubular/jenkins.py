"""
Methods to interact with the Jenkins API to perform various tasks.
"""
import logging
import math

import backoff
from jenkinsapi.jenkins import Jenkins
from jenkinsapi.custom_exceptions import JenkinsAPIException
from requests.exceptions import HTTPError

from tubular.exception import BackendError

LOG = logging.getLogger(__name__)


def _poll_giveup(data):
    u""" Raise an error when the polling tries are exceeded."""
    orig_args = data.get(u'args')
    # The Build object was the only parameter to the original method call,
    # and so it's the first and only item in the args.
    build = orig_args[0]
    msg = u'Timed out waiting for build {} to finish.'.format(build.name)
    raise BackendError(msg)


def _backoff_timeout(timeout, base=2, factor=1):
    u"""
    Return a tuple of (wait_gen, max_tries) so that backoff will only try up to `timeout` seconds.

    |timeout|wait x sec|make attempt #|total time (sec)|total time (min)|
    |----:|----:|---:|----:|-----:|
    |0    |0    |1   |0    | 0    |
    |0.1  |1    |2   |1    |0.02  |
    |0.5  |2    |3   |3    |0.05  |
    |4    |4   |7    |0.12  |
    |8    |5   |15   |0.25  |
    |16   |6   |31   |0.52  |
    |32   |7   |63   |1.05  |
    |64   |8   |127  |2.12  |
    |128  |9   |255  |4.25  |
    |256  |10  |511  |8.52  |
    |512  |11  |1023 |17.05 |
    |1024 |12  |2047 |34.12 |
    |2048 |13  |4095 |68.25 |

    """
    # Total duration of sum(factor * base ** n for n in range(K)) = factor*(base**K - 1)/(base - 1),
    # where K is the number of retries, or max_tries - 1 (since the first try doesn't require a wait)
    #
    # Solving for K, K = log(timeout * (base - 1) / factor + 1, base)
    #
    # Using the next smallest integer K will give us a number of elements from
    # the exponential sequence to take and still be less than the timeout.
    tries = int(math.log(timeout * (base - 1) / factor + 1, base))

    remainder = timeout - (factor * (base ** tries - 1)) / (base - 1)

    def expo():
        u"""Compute an exponential backoff wait period, but capped to an expected max timeout"""
        # pylint: disable=invalid-name
        n = 0
        while True:
            a = factor * base ** n
            if n >= tries:
                yield remainder
            else:
                yield a
                n += 1

    # tries tells us the largest standard wait using the standard progression (before being capped)
    # tries + 1 because backoff waits one fewer times than max_tries (the first attempt has no wait time).
    # If a remainder, then we need to make one last attempt to get the target timeout (so tries + 2)
    if remainder == 0:
        return expo, tries + 1
    else:
        return expo, tries + 2


def trigger_build(base_url, user_name, user_token, job_name, job_token,
                  job_cause=None, job_params=None, timeout=60 * 30):
    u"""
    Trigger a jenkins job/project (note that jenkins uses these terms interchangeably)

    Args:
        base_url (str): The base URL for the jenkins server, e.g. https://test-jenkins.testeng.edx.org
        user_name (str): The jenkins username
        user_token (str): API token for the user. Available at {base_url}/user/{user_name)/configure
        job_name (str): The Jenkins job name, e.g. test-project
        job_token (str): Jobs must be configured with the option "Trigger builds remotely" selected.
            Under this option, you must provide an authorization token (configured in the job)
            in the form of a string so that only those who know it would be able to remotely
            trigger this project's builds.
        job_cause (str): Text that will be included in the recorded build cause
        job_params (set of tuples): Parameter names and their values to pass to the job
        timeout (int): The maximum number of seconds to wait for the jenkins build to complete (measured
            from when the job is triggered.)

    Returns:
        A the status of the build that was triggered

    Raises:
        BackendError: if the Jenkins job could not be triggered successfully
    """
    wait_gen, max_tries = _backoff_timeout(timeout)

    @backoff.on_predicate(
        wait_gen,
        max_tries=max_tries,
        on_giveup=_poll_giveup
    )
    def poll_build_for_result(build):
        u"""
        Poll for the build running, with exponential backoff, capped to ``timeout`` seconds.
        The on_predicate decorator is used to retry when the return value
        of the target function is True.
        """
        return not build.is_running()

    # Create a dict with key/value pairs from the job_params
    # that were passed in like this:  --param FOO bar --param BAZ biz
    # These will get passed to the job as string parameters like this:
    # {u'FOO': u'bar', u'BAX': u'biz'}
    request_params = {}
    for param in job_params:
        request_params[param[0]] = param[1]

    # Contact jenkins, log in, and get the base data on the system.
    try:
        jenkins = Jenkins(base_url, username=user_name, password=user_token)
    except (JenkinsAPIException, HTTPError) as err:
        raise BackendError(str(err))

    if not jenkins.has_job(job_name):
        msg = u'Job not found: {}.'.format(job_name)
        msg += u' Verify that you have permissions for the job and double check the spelling of its name.'
        raise BackendError(msg)

    # This will start the job and will return a QueueItem object which can be used to get build results
    job = jenkins[job_name]
    queue_item = job.invoke(securitytoken=job_token, build_params=request_params, cause=job_cause)
    LOG.info(u'Added item to jenkins. Server: {} Job: {} '.format(
        jenkins.base_server_url(), queue_item
    ))

    # Block this script until we are through the queue and the job has begun to build.
    queue_item.block_until_building()
    build = queue_item.get_build()
    LOG.info(u'Created build {}'.format(build))
    LOG.info(u'See {}'.format(build.baseurl))

    # Now block until you get a result back from the build.
    poll_build_for_result(build)
    status = build.get_status()
    LOG.info(u'Build status: {status}'.format(status=status))
    return status

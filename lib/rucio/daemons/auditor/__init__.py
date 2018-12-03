# Copyright 2015-2018 CERN for the benefit of the ATLAS collaboration.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Authors:
# - Fernando Lopez <fernando.e.lopez@gmail.com>, 2015-2016
# - Martin Barisits <martin.barisits@cern.ch>, 2017
# - Mario Lassnig <mario.lassnig@cern.ch>, 2017
# - Vincent Garonne <vgaronne@gmail.com>, 2018
# - Dimitrios Christidis <dimitrios.christidis@cern.ch>, 2018

import Queue
import bz2
import glob
import logging
import os
import select
import sys

from datetime import datetime
from datetime import timedelta
from rucio.common import config
from rucio.common.dumper import LogPipeHandler
from rucio.common.dumper import mkdir
from rucio.common.dumper import temp_file
from rucio.common.dumper.consistency import Consistency
from rucio.core.quarantined_replica import add_quarantined_replicas
from rucio.core.rse import get_rse_usage
from rucio.daemons.auditor.hdfs import ReplicaFromHDFS
from rucio.daemons.auditor import srmdumps


def consistency(rse, delta, configuration, cache_dir, results_dir):
    logger = logging.getLogger('auditor-worker')
    rsedump, rsedate = srmdumps.download_rse_dump(rse, configuration, destdir=cache_dir)
    results_path = os.path.join(results_dir, '{0}_{1}'.format(rse, rsedate.strftime('%Y%m%d')))  # pylint: disable=no-member

    if os.path.exists(results_path + '.bz2') or os.path.exists(results_path):
        logger.warn('Consistency check for "%s" (dump dated %s) already done, skipping check', rse, rsedate.strftime('%Y%m%d'))  # pylint: disable=no-member
        return None

    rrdump_prev = ReplicaFromHDFS.download(rse, rsedate - delta, cache_dir=cache_dir)
    rrdump_next = ReplicaFromHDFS.download(rse, rsedate + delta, cache_dir=cache_dir)
    results = Consistency.dump(
        'consistency-manual',
        rse,
        rsedump,
        rrdump_prev,
        rrdump_next,
        date=rsedate,
        cache_dir=cache_dir,
    )
    mkdir(results_dir)
    with temp_file(results_dir, results_path) as (output, _):
        for result in results:
            output.write('{0}\n'.format(result.csv()))

    return results_path


def guess_replica_info(path):
    """Try to extract the scope and name from a path.

    ``path`` should be an ``str`` with the relative path to the file on
    the RSE.

    Returns a ``tuple`` of which the first element is the scope of the
    replica and the second element is its name.
    """
    items = path.split('/')
    if len(items) == 1:
        return None, path
    elif len(items) > 2 and items[0] in ['group', 'user']:
        return '.'.join(items[0:2]), items[-1]
    else:
        return items[0], items[-1]


def bz2_compress_file(source, chunk_size=65000):
    """Compress a file with bzip2.

    The destination is the path passed through ``source`` extended with
    '.bz2'.  The original file is deleted.

    Errors are deliberately not handled gracefully.  Any exceptions
    should be propagated to the caller.

    ``source`` should be an ``str`` with the absolute path to the file
    to compress.

    ``chunk_size`` should be an ``int`` with the size (in bytes) of the
    chunks by which to read the file.

    Returns an ``str`` with the destination path.
    """
    destination = '{}.bz2'.format(source)
    with open(source) as plain, bz2.BZ2File(destination, 'w') as compressed:
        while True:
            chunk = plain.read(chunk_size)
            if not chunk:
                break
            compressed.write(chunk)
    os.remove(source)
    return destination


def process_output(output, sanity_check=True, compress=True):
    """Perform post-consistency-check actions.

    DARK files are put in the quarantined-replica table so that they
    may be deleted by the Dark Reaper.  LOST files are currently
    ignored.

    ``output`` should be an ``str`` with the absolute path to the file
    produced by ``consistency()``.  It must maintain its naming
    convention.

    If ``sanity_check`` is ``True`` (default) and the number of entries
    in the output file is deemed excessive, the actions are aborted.

    If ``compress`` is ``True`` (default), the file is compressed with
    bzip2 after the actions are successfully performed.
    """
    logger = logging.getLogger('auditor-worker')
    dark_replicas = []
    try:
        with open(output) as f:
            for line in f:
                label, path = line.rstrip().split(',', 1)
                if label == 'DARK':
                    scope, name = guess_replica_info(path)
                    dark_replicas.append({'path': path,
                                          'scope': scope,
                                          'name': name})
                elif label == 'LOST':
                    # TODO: Declare LOST files as suspicious.
                    pass
                else:
                    raise ValueError('unexpected label')
    # Since the file is read immediately after its creation, any error
    # exposes a bug in the Auditor.
    except Exception as error:
        logger.critical('Error processing "%s"', output, exc_info=True)
        raise error

    rse = os.path.basename(output[:output.rfind('_')])
    usage = get_rse_usage(rse, source='rucio')[0]
    threshold = config.config_get('auditor', 'threshold', False, 0.2)

    # Perform a basic sanity check by comparing the number of entries
    # with the total number of files on the RSE.  If the percentage is
    # significant, there is most likely an issue with the site dump.
    if sanity_check and len(dark_replicas) > threshold * usage['files']:
        raise AssertionError('number of DARK files is exceeding threshold')

    add_quarantined_replicas(rse, dark_replicas)
    logger.debug('Processed %d DARK files from "%s"', len(dark_replicas),
                 output)

    if compress:
        destination = bz2_compress_file(output)
        logger.debug('Compressed "%s"', destination)


def check(queue, retry, terminate, logpipe, cache_dir, results_dir, keep_dumps, delta_in_days):
    logger = logging.getLogger('auditor-worker')
    lib_logger = logging.getLogger('dumper')

    loglevel = logging.getLevelName(config.config_get('common', 'loglevel', False, 'DEBUG'))
    logger.setLevel(loglevel)
    lib_logger.setLevel(loglevel)

    handler = LogPipeHandler(logpipe)
    logger.addHandler(handler)
    lib_logger.addHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s  %(name)-22s  %(levelname)-8s [PID %(process)8d] %(message)s"
    )
    handler.setFormatter(formatter)

    delta = timedelta(days=delta_in_days)

    configuration = srmdumps.parse_configuration()

    while not terminate.is_set():
        try:
            rse, attemps = queue.get(timeout=30)
        except Queue.Empty:
            continue

        start = datetime.now()
        try:
            logger.debug('Checking "%s"', rse)
            output = consistency(rse, delta, configuration, cache_dir,
                                 results_dir)
            if output:
                process_output(output)
        except:
            success = False
        else:
            success = True
        finally:
            elapsed = (datetime.now() - start).total_seconds() / 60
            if success:
                logger.info('SUCCESS checking "%s" in %d minutes', rse, elapsed)
            else:
                class_, desc = sys.exc_info()[0:2]
                logger.error('Check of "%s" failed in %d minutes, %d remaining attemps: (%s: %s)', rse, elapsed, attemps, class_.__name__, desc)

        if not keep_dumps:
            remove = glob.glob(os.path.join(cache_dir, 'replicafromhdfs_{0}_*'.format(rse)))
            remove.extend(glob.glob(os.path.join(cache_dir, 'ddmendpoint_{0}_*'.format(rse))))
            logger.debug('Removing: %s', remove)
            for fil in remove:
                os.remove(fil)

        if not success and attemps > 0:
            retry.put((rse, attemps - 1))


def activity_logger(logpipes, logfilename, terminate):
    handler = logging.handlers.RotatingFileHandler(
        logfilename,
        maxBytes=20971520,
        backupCount=10,
    )
    handler.setFormatter(logging.Formatter(fmt=None))
    logger = logging.getLogger('auditor-logger-raw')
    logger.addHandler(handler)
    logger.setLevel(logging.CRITICAL)  # The level of this logger is irrelevant

    while not terminate.is_set():
        ready, _, _ = select.select(logpipes, tuple(), tuple(), 30)
        if ready:
            for logpipe in ready:
                logger.critical(logpipe.recv())

"""Bounded transfers for the pinned sequential publisher.

publish_termination_release.py runs one `aws` process at a time. It uploads
manifest.json only after every other object has been stored and verified, and
it aborts without a pointer write when any call fails. A release is about
12,000 small objects, so the wait inside each process is the cost.

This pool accepts those non-manifest puts before the bytes are on the
service, runs a fixed number of transfers, and reports a stored failure on
the next call. The manifest put waits until every accepted put has
succeeded, and it is not sent when one has failed. Verification reads use
the same worker cap.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath


DEFAULT_CONCURRENCY = 32
MAX_CONCURRENCY = 64


class PoolError(RuntimeError):
    pass


def concurrency_from_env(raw):
    """Return the worker count. `raw` is the SCIMAP_PUBLISH_CONCURRENCY value or None."""
    text = str(DEFAULT_CONCURRENCY) if raw is None else str(raw).strip()
    if not text:
        text = str(DEFAULT_CONCURRENCY)
    if not text.isdigit() or not 1 <= int(text) <= MAX_CONCURRENCY:
        raise ValueError(
            f'SCIMAP_PUBLISH_CONCURRENCY must be an integer from 1 to {MAX_CONCURRENCY}'
        )
    return int(text)


class TransferPool:
    """One shared transfer cap for puts and the verification reads that follow."""

    def __init__(self, concurrency, operation):
        if not isinstance(concurrency, int) or not 1 <= concurrency <= MAX_CONCURRENCY:
            raise ValueError(f'concurrency must be an integer from 1 to {MAX_CONCURRENCY}')
        self.concurrency = concurrency
        self.max_in_flight = 0
        self._operation = operation
        self._sem = threading.BoundedSemaphore(concurrency)
        self._executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='scimap-r2')
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending = 0
        self._in_flight = 0
        self._errors = []
        self._known = set()
        self._jobs = {}
        self._submit_order = []
        self._results = {}
        self._completed = []
        self._downloads = {}
        self._prefetch_started = False

    def stats(self):
        with self._lock:
            return {
                'concurrency': self.concurrency,
                'max_in_flight': self.max_in_flight,
                'uploaded': len(self._completed),
                'accepted': len(self._submit_order),
                'errors': len(self._errors),
            }

    def idle(self):
        with self._lock:
            if self._pending or self._in_flight:
                return False
            return all(slot['done'] for slot in self._downloads.values())

    def close(self, wait=False):
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def call(self, method, endpoint, region, bucket, key, query, headers, body):
        """Run one transfer inside the worker cap. The operation raises on HTTP failure."""
        self._sem.acquire()
        try:
            with self._lock:
                self._in_flight += 1
                if self._in_flight > self.max_in_flight:
                    self.max_in_flight = self._in_flight
            try:
                return self._operation(method, endpoint, region, bucket, key, query, headers, body)
            finally:
                with self._lock:
                    self._in_flight -= 1
        finally:
            self._sem.release()

    def put(self, job):
        """Store one object. manifest.json waits; every other key is queued."""
        if PurePosixPath(job['key']).name == 'manifest.json':
            self.put_manifest(job)
            return 'manifest'
        self.submit_put(job)
        return 'accepted'

    def submit_put(self, job):
        """Enqueue a non-manifest put and return before the service responds."""
        if PurePosixPath(job['key']).name == 'manifest.json':
            message = 'manifest.json cannot use the early-ack put path'
            with self._lock:
                self._errors.append(message)
            raise PoolError(message)
        try:
            self._check_put(job)
        except PoolError as error:
            with self._lock:
                self._errors.append(str(error))
            raise
        key = job['key']
        with self._cv:
            if self._errors:
                raise PoolError(self._errors[0])
            if key in self._known:
                message = f'refusing duplicate put for {key}'
                self._errors.append(message)
                raise PoolError(message)
            self._known.add(key)
            self._jobs[key] = job
            self._submit_order.append(key)
            self._pending += 1
        try:
            self._executor.submit(self._run_put, job)
        except RuntimeError as error:
            with self._cv:
                self._pending -= 1
                self._errors.append(str(error))
                self._cv.notify_all()
            raise PoolError(str(error)) from error

    def put_manifest(self, job):
        """Upload manifest.json only after every accepted put has succeeded."""
        if PurePosixPath(job['key']).name != 'manifest.json':
            raise PoolError('manifest upload requires a manifest.json key')
        try:
            self._check_put(job)
        except PoolError as error:
            with self._lock:
                self._errors.append(str(error))
            raise
        self._wait_pending()
        with self._cv:
            if self._errors:
                raise PoolError(self._errors[0])
            if job['key'] in self._known:
                message = f'refusing duplicate put for {job["key"]}'
                self._errors.append(message)
                raise PoolError(message)
            self._known.add(job['key'])
            self._jobs[job['key']] = job
        try:
            self._put(job)
        except Exception as error:
            with self._lock:
                self._errors.append(str(error))
            raise
        with self._lock:
            self._completed.append(job['key'])

    def wait_for_puts(self):
        """Block until accepted puts finish, then start verification reads."""
        self._wait_pending()
        self._start_prefetch()

    def read_object(self, job):
        """Return (metadata, was_prefetched) after the object bytes are on disk."""
        self.wait_for_puts()
        key = job['key']
        with self._cv:
            slot = self._downloads.get(key)
            if slot is not None:
                while not slot['done']:
                    self._cv.wait()
                if slot['error']:
                    raise PoolError(slot['error'])
                payload = slot['data']
                headers = slot['headers']
                slot['data'] = None
                direct = False
            else:
                direct = True
        if direct:
            headers, payload = self.call(
                'GET', job['endpoint'], job['region'], job['bucket'], key, {}, {}, None,
            )
        self._write(job['outfile'], payload)
        return {'ContentLength': len(payload), 'ETag': headers.get('etag', '')}

    def _check_put(self, job):
        if job.get('if_none_match') != '*' or not job.get('content_md5'):
            raise PoolError('refusing put without --if-none-match * and --content-md5')
        if not job.get('key') or not job.get('bucket') or not job.get('endpoint'):
            raise PoolError('refusing put without bucket, key, and endpoint')

    def _put(self, job):
        self._check_put(job)
        headers = {
            'content-md5': job['content_md5'],
            'content-type': 'application/octet-stream',
            'if-none-match': job['if_none_match'],
        }
        self.call(
            'PUT', job['endpoint'], job['region'], job['bucket'], job['key'], {}, headers, job['body'],
        )

    def _run_put(self, job):
        error = None
        try:
            with self._lock:
                if self._errors:
                    error = PoolError(self._errors[0])
            if error is None:
                self._put(job)
        except Exception as exc:
            error = exc
        with self._cv:
            self._pending -= 1
            if error is not None:
                self._errors.append(str(error))
                self._results[job['key']] = str(error)
            else:
                self._results[job['key']] = None
                self._completed.append(job['key'])
            self._cv.notify_all()

    def _wait_pending(self):
        with self._cv:
            while self._pending:
                self._cv.wait()
            if self._errors:
                raise PoolError(self._errors[0])

    def _start_prefetch(self):
        with self._lock:
            if self._prefetch_started or self._errors or not self._submit_order:
                return
            self._prefetch_started = True
            keys = list(self._submit_order)
            for key in keys:
                self._downloads[key] = {'done': False, 'error': None, 'data': None, 'headers': {}}
        for key in keys:
            self._executor.submit(self._prefetch, key)

    def _prefetch(self, key):
        job = self._jobs[key]
        error = None
        payload = b''
        headers = {}
        try:
            with self._lock:
                if self._errors:
                    raise PoolError(self._errors[0])
            headers, payload = self.call(
                'GET', job['endpoint'], job['region'], job['bucket'], key, {}, {}, None,
            )
        except Exception as exc:
            error = exc
        with self._cv:
            slot = self._downloads[key]
            slot['done'] = True
            if error is None:
                slot['data'] = payload
                slot['headers'] = headers or {}
            else:
                slot['error'] = str(error)
                self._errors.append(str(error))
            self._cv.notify_all()

    def _write(self, path, payload):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.partial')
        temporary.write_bytes(payload)
        temporary.replace(target)

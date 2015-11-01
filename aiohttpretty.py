import sys
import copy
import json
import pprint
import asyncio
import collections

import furl
import aiohttp
import aiohttp.streams


class ImmutableFurl:

    def __init__(self, url, params=None):
        self._url = url
        self._furl = furl.furl(url)
        self._params = furl.furl(url).args
        self._furl.set(args={})

        params = params or {}
        for (k, v) in params.items():
            self._params.add(k, v)

    def with_out_params(self):
        return ImmutableFurl(self.url)

    @property
    def url(self):
        return self._furl.url

    @property
    def params(self):
        return self._params

    def __eq__(self, other):
        return hash(self) == hash(other)

    def show_params(self):
        return repr(self.params).replace(' ', '')

    def get_hashable(self):
        return self.url + ''.join([
            self.params[x] or ''
            for x in sorted(self.params)
        ])

    def __hash__(self):
        return hash(self.get_hashable())

    def __str__(self):
        return self.get_hashable()

class _MockStream(aiohttp.streams.StreamReader):
    def __init__(self, data):
        super().__init__()
        if isinstance(data, str):
            data = data.encode('UTF-8')
        elif not isinstance(data, bytes):
            raise TypeError('Data must be either str or bytes, found {!r}'.format(type(data)))

        self.size = len(data)
        self.feed_data(data)
        self.feed_eof()


def _wrap_content_stream(content):
    if isinstance(content, str):
        content = content.encode('utf-8')

    if isinstance(content, bytes):
        return _MockStream(content)

    if hasattr(content, 'read') and asyncio.iscoroutinefunction(content.read):
        return content

    raise TypeError('Content must be of type bytes or str, or implement the stream interface.')


class _AioHttPretty:
    def __init__(self):
        self.calls = []
        self.registry = {}
        self.request = None

    def make_call(self, **kwargs):
        return kwargs

    @asyncio.coroutine
    def process_request(self, **kwargs):
        """Process request options as if the request was actually executed."""
        data = kwargs.get('data')
        if isinstance(data, asyncio.StreamReader):
            yield from data.read()

    @asyncio.coroutine
    def fake_request(self, method, uri, **kwargs):
        params = kwargs.get('params', None)
        url = ImmutableFurl(uri, params=params)

        pprint.pprint("********  FakeRequest **********")
        pprint.pprint("***GIVEN_URI:" + str(uri))
        pprint.pprint("***GIVEN_PARAMS:" + repr(params))
        pprint.pprint("***REAL_URL:" + str(url._furl))
        pprint.pprint("***PARAMS:" + url.show_params())
        pprint.pprint("***HASHABLE_URL:" + url.get_hashable())
        pprint.pprint("***TUPLE_HASHVAL:" + str(hash((method, url))))
        pprint.pprint("******** /FakeRequest **********")

        try:
            response = self.registry[(method, url)]
        except KeyError:
            raise Exception('No URLs matching {method} {uri}. Not making request. Go fix your test.'.format(**locals()))

        if isinstance(response, collections.Sequence):
            try:
                response = response.pop(0)
            except IndexError:
                raise Exception('No responses left.')

        yield from self.process_request(**kwargs)

        post_params = kwargs.pop('params', None)
        call_url = ImmutableFurl(uri, params=post_params)
        call = self.make_call(method=method, uri=call_url, **kwargs)
        self.calls.append(call)

        pprint.pprint("********  RegisterCall **********")
        pprint.pprint("***GIVEN_URI:" + str(uri))
        pprint.pprint("***GIVEN_PARAMS:" + repr(post_params))
        pprint.pprint("***REAL_URL:" + str(call_url))
        pprint.pprint("***KWARGS:" + repr(kwargs))
        pprint.pprint("***CALL:" + repr(call))
        pprint.pprint("******** /RegisterCall **********")

        mock_response = aiohttp.client.ClientResponse(method, uri)
        mock_response.content = _wrap_content_stream(response.get('body', 'aiohttpretty'))

        if response.get('auto_length'):
            defaults = {
                'Content-Length': str(mock_response.content.size)
            }
        else:
            defaults = {}

        mock_response.headers = aiohttp.multidict.CIMultiDict(response.get('headers', defaults))
        mock_response.status = response.get('status', 200)
        return mock_response

    def register_uri(self, method, uri, **options):
        if any(x.get('params') for x in options.get('responses', [])):
                raise ValueError('Cannot specify params in responses, call register multiple times.')

        params = options.pop('params', {})
        url = ImmutableFurl(uri, params=params)

        self.registry[(method, url)] = options.get('responses', options)

        pprint.pprint("############  REGISTER ###########")
        pprint.pprint("###GIVEN_URI:" + str(uri))
        pprint.pprint("###GIVEN_PARAMS:" + repr(params))
        pprint.pprint("###REAL_URL:" + str(url._furl))
        pprint.pprint("###PARAMS:" + url.show_params())
        pprint.pprint("###HASHABLE_URL:" + url.get_hashable())
        pprint.pprint("###TUPLE_HASHVAL:" + str(hash((method, url))))
        pprint.pprint("############ /REGISTER ###########")

    def register_json_uri(self, method, uri, **options):
        body = json.dumps(options.pop('body', None)).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        headers.update(options.pop('headers', {}))
        self.register_uri(method, uri, body=body, headers=headers, **options)

    def activate(self):
        aiohttp.request, self.request = self.fake_request, aiohttp.request

    def deactivate(self):
        aiohttp.request, self.request = self.request, None

    def clear(self):
        self.calls = []
        self.registry = {}

    def compare_call(self, first, second):
        for key, value in first.items():
            pprint.pprint("~~~~~  CallCompare ~~~~~~~")
            pprint.pprint("~~~K: " + key)
            if second.get(key) != value:
                pprint.pprint("~~~V: first: " + str(value) + "    second: " + str(second.get(key)))
                pprint.pprint("~~~~~ /CallCompare ~~~~~~~")
                return False
        return True

    def has_call(self, uri, check_params=True, **kwargs):
        params = kwargs.pop('params', None)
        call_url = ImmutableFurl(uri, params=params)

        pprint.pprint("########  HasCall ########")
        pprint.pprint("###GIVEN_URI:" + str(uri))
        pprint.pprint("###GIVEN_PARAMS:" + repr(params))
        pprint.pprint("###REAL_URL:" + str(call_url))
        pprint.pprint("###KWARGS:" + repr(kwargs))

        kwargs['uri'] = call_url

        ret = False
        for call in self.calls:
            pprint.pprint("###CALL:" + repr(call))

            if not check_params:
                call = copy.deepcopy(call)
                call['uri'] = call['uri'].with_out_params()
            if self.compare_call(kwargs, call):
                ret = True

        pprint.pprint("######## /HasCall ########")

        return ret


sys.modules[__name__] = _AioHttPretty()

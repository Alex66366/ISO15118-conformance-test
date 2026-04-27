from django.urls import re_path

from testcase.consumers import LoggingConsumer

channel_routing = [
    re_path(r'ws/logging/?$', LoggingConsumer.as_asgi()),
]
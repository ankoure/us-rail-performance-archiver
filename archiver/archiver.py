from archiver.response import parse_response
from archiver.feed import Feed
from archiver.writer import LocalWriter


class FeedArchiver:
    def __init__(self, feeds: list[Feed], writer: LocalWriter):
        self.feeds = feeds
        self.writer = writer

    def archive_once(self):
        for feed in self.feeds:
            response = parse_response(feed.client.get(feed.path), feed.expected_format)
            self.writer.write(feed.name, response)

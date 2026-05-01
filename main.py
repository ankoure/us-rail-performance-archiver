from archiver.auth import APIClient
from archiver.archiver import FeedArchiver
from archiver.feed import Feed
from archiver.writer import LocalWriter


def main():
    client = APIClient("http://api.bart.gov")
    feed = Feed(name="bart-trips", path="/gtfsrt/tripupdate.aspx", client=client)
    archiver = FeedArchiver(feeds=[feed], writer=LocalWriter("./archive"))
    archiver.archive_once()


if __name__ == "__main__":
    main()

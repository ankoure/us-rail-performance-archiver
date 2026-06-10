class FakeSink:
    def __init__(self):
        self.puts: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self.puts[key] = data

from pathlib import Path

from mathpix_pipeline.client import MathpixClient


class Response:
    def __init__(self, data, status_code=200, content=b"payload"):
        self._data = data
        self.status_code = status_code
        self.content = content
        self.text = str(data)
        self.ok = status_code < 400

    def json(self):
        return self._data


class Session:
    def __init__(self):
        self.states = [
            {"status": "processing", "conversion_status": {}},
            {
                "status": "completed",
                "conversion_status": {"pdf": {"status": "completed"}},
            },
        ]

    def post(self, *args, **kwargs):
        if args[0].endswith("/v3/converter"):
            return Response({"conversion_id": "conversion-1"})
        return Response({"pdf_id": "job-1"})

    def get(self, url, **kwargs):
        if url.endswith("/conversion-1"):
            return Response(
                {"status": "completed", "conversion_status": {"pdf": {"status": "completed"}}}
            )
        if url.endswith("/job-1"):
            return Response(self.states.pop(0))
        return Response({}, content=b"pdf bytes")


def test_submit_wait_and_download(tmp_path):
    session = Session()
    client = MathpixClient("id", "key", session=session)
    source = tmp_path / "in.pdf"
    source.write_bytes(b"input")
    job = client.submit(source, {"conversion_formats": {"pdf": True}})
    state = client.wait(job, ["pdf"], poll_seconds=0, max_wait_seconds=1)
    assert state["status"] == "completed"
    destination = tmp_path / "out.pdf"
    client.download(job, "pdf", destination)
    assert destination.read_bytes() == b"pdf bytes"


def test_rerender_submit_wait_and_download(tmp_path):
    session = Session()
    client = MathpixClient("id", "key", session=session)
    conversion = client.submit_conversion("text", {"pdf": True}, {"pdf": {"fontSize": 12}})
    state = client.wait_conversion(conversion, ["pdf"], poll_seconds=0, max_wait_seconds=1)
    assert state["conversion_status"]["pdf"]["status"] == "completed"
    destination = tmp_path / "rerender.pdf"
    client.download_conversion(conversion, "pdf", destination)
    assert destination.read_bytes() == b"pdf bytes"

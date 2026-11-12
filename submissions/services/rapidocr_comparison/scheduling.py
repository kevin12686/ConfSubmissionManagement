"""Bounded document-local dynamic dispatch, with ordered all-or-error results."""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


class RecognizerPool:
    def __init__(self, engines):
        if not engines or len({id(engine) for engine in engines}) != len(engines):
            raise ValueError("OCR requires independent recognizers for every lane.")
        self.engines = engines

    def _load_det_model(self):
        return self.engines[0]._load_det_model()

    def _load_rec_model(self):
        return self.engines[0]._load_rec_model()

    def extract(self, data):
        from . import runtime
        # One executor per engine prevents concurrent use of RapidOCR's mutable
        # detector/recognizer state. PyMuPDF remains on the calling thread.
        executors = [ThreadPoolExecutor(max_workers=1) for _ in self.engines]
        free = deque(range(len(self.engines)))
        pending, completed = {}, {}
        characters = 0
        expected = 0

        def collect():
            nonlocal characters
            ready, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in ready:
                slot, metadata, geometry = pending.pop(future)
                ocr = future.result()
                number = metadata["page"]
                if number in completed:
                    raise ValueError("Duplicate OCR page result.")
                characters += sum(len(line["text"]) for line in ocr["lines"])
                if characters > runtime.MAX_CHARACTERS:
                    raise ValueError("OCR character work limit exceeded.")
                completed[number] = ({**metadata, "ocr": ocr}, geometry)
                free.append(slot)

        frames = runtime.rendered_pages(data)
        try:
            # Do not render another full-size image while every engine is busy.
            while True:
                if not free:
                    collect()
                try:
                    metadata, geometry, image = next(frames)
                except StopIteration:
                    break
                expected += 1
                if metadata["page"] != expected:
                    raise ValueError("Invalid OCR input page order.")
                slot = free.popleft()
                future = executors[slot].submit(runtime.recognize_page, self.engines[slot], image,
                                                 metadata["width"], metadata["height"])
                pending[future] = (slot, metadata, geometry)
            while pending:
                collect()
            if not expected or set(completed) != set(range(1, expected + 1)):
                raise ValueError("Incomplete OCR document.")
            ordered = [completed[number] for number in range(1, expected + 1)]
            return [pair[0] for pair in ordered], [pair[1] for pair in ordered]
        finally:
            frames.close()
            for future in pending:
                future.cancel()
            for executor in executors:
                executor.shutdown(wait=True, cancel_futures=True)

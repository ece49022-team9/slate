import logging
import queue
import threading

logger = logging.getLogger("slate.voice.queues")


class QueueSender:
    def __init__(self, outgoing) -> None:
        self.outgoing = outgoing
        self.pending = queue.SimpleQueue()
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def put(self, value: bytes | str) -> None:
        self.pending.put(value)

    def run(self) -> None:
        finished = False
        while not finished:
            values = [self.pending.get()]
            while not self.pending.empty():
                values.append(self.pending.get())
            finished = values[-1] is None
            values = [value for value in values if value is not None]
            if not values or self.error is not None:
                continue
            try:
                self.outgoing.put(type(values[0])().join(values))
            except Exception as error:
                logger.exception("slate.voice: sending to the caller's queue failed")
                self.error = error

    def close(self) -> None:
        self.pending.put(None)
        self.thread.join()
        if self.error is not None:
            raise RuntimeError("slate.voice: streamed output was lost") from self.error

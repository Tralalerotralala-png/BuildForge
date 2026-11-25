import queue
import unittest
from types import SimpleNamespace

from app import App


class DashboardQueueTests(unittest.TestCase):
    def test_backlog_yields_and_preserves_fifo(self):
        events = queue.Queue()
        for i in range(205):
            events.put(("line", str(i)))
        rendered = []
        scheduled = []
        app = SimpleNamespace(q=events, append=rendered.append,
                              root=SimpleNamespace(after=lambda delay, cb: scheduled.append(delay)))
        app.tick = lambda: App.tick(app)
        app.tick()
        self.assertEqual(rendered, [str(i) for i in range(100)])
        self.assertEqual(scheduled, [1])
        app.tick()
        app.tick()
        self.assertEqual(rendered, [str(i) for i in range(205)])
        self.assertEqual(scheduled, [1, 1, 150])

    def test_empty_queue_uses_normal_poll_interval(self):
        scheduled = []
        app = SimpleNamespace(q=queue.Queue(),
                              root=SimpleNamespace(after=lambda delay, cb: scheduled.append(delay)))
        app.tick = lambda: App.tick(app)
        app.tick()
        self.assertEqual(scheduled, [150])


if __name__ == "__main__":
    unittest.main()

import unittest

from beanoflight.events import BeanStore, Enrichment, EventBus
from beanoflight.models import BeanEvent, BeanRef


class EventContractTests(unittest.TestCase):
    def test_store_keeps_versioned_async_enrichments(self):
        store = BeanStore()
        bean = BeanRef("run", 4)
        store.add(bean, Enrichment("resnet", "defect", "weevil", 10, "model-v2"))
        result = store.snapshot(bean)
        self.assertEqual(result["defect"][0].value, "weevil")
        self.assertEqual(result["defect"][0].version, "model-v2")

    def test_bounded_bus_discards_oldest_for_slow_consumer(self):
        bus = EventBus()
        subscriber = bus.subscribe(capacity=1)
        bean = BeanRef("run", 1)
        bus.publish(BeanEvent("created", bean, 1))
        bus.publish(BeanEvent("confirmed", bean, 2))
        self.assertEqual(subscriber.get_nowait().kind, "confirmed")


if __name__ == "__main__":
    unittest.main()

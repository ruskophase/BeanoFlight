.PHONY: test run

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

run:
	PYTHONPATH=src python3 -m beanoflight

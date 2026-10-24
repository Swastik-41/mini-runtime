CXX := g++
CXXFLAGS := -std=c++17 -Wall -Wextra -O2

TARGET := mini-runtime

.PHONY: all clean setup test benchmark dashboard dashboard-test

all: $(TARGET)

$(TARGET): src/main.cpp
	$(CXX) $(CXXFLAGS) src/main.cpp -o $(TARGET)

setup:
	./scripts/setup.sh

test: $(TARGET)
	./tests/run_tests.sh

benchmark: $(TARGET)
	./tests/benchmark.sh

dashboard: $(TARGET)
	python3 dashboard/server.py

dashboard-test:
	python3 -m unittest discover -s dashboard/tests -v

clean:
	rm -f $(TARGET)
	rm -f tests/memory_hog

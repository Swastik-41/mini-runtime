#include <vector>
#include <memory>
#include <thread>
#include <chrono>
#include <cstring>
#include <iostream>

// Allocates 200 MB in 10 MB steps, slowly enough that the climb towards the
// memory limit is visible in the dashboard before the OOM killer stops it.
int main() {
    constexpr std::size_t step = 10 * 1024 * 1024;
    std::vector<std::unique_ptr<char[]>> blocks;

    for (int mb = 10; mb <= 200; mb += 10) {
        blocks.emplace_back(new char[step]);
        std::memset(blocks.back().get(), 1, step);
        std::cout << "Allocated " << mb << " MB" << std::endl;
        std::this_thread::sleep_for(std::chrono::milliseconds(300));
    }

    std::cout << "Allocated 200 MB\n";
    std::cin.get();
}

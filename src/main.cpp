#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <cstdlib>
#include <cstring>
#include <cerrno>
#include <new>
#include <filesystem>
#include <cctype>

#include <sched.h>
#include <unistd.h>
#include <sys/wait.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/syscall.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/prctl.h>
#include <net/if.h>
#include <linux/capability.h>
#include <signal.h>
#include <pwd.h>
#include <fcntl.h>
#include <sys/file.h>

constexpr int STACK_SIZE = 1024 * 1024;
namespace fs = std::filesystem;

const std::string CGROUP_ROOT = "/sys/fs/cgroup";

struct ResourceLimits {
    std::string memory = "100M";
    int cpuPercent = 50;
    int pids = 64;
};

struct RunOptions {
    ResourceLimits limits;
    std::string image;               // empty = keep stored image or use "base"
    bool network = false;            // create a veth pair to the host
    std::vector<std::string> command{"/bin/sh"};
};

std::string getRuntimeDir() {
    // Under sudo, keep using the invoking user's directory. Only trust
    // SUDO_USER when we really are root: "sudo -u alice" sets it too.
    const char* sudoUser = std::getenv("SUDO_USER");

    if (sudoUser && geteuid() == 0) {
        struct passwd* pw = getpwnam(sudoUser);
        if (pw)
            return std::string(pw->pw_dir) + "/.mini-runtime";
    }

    const char* home = std::getenv("HOME");
    if (home)
        return std::string(home) + "/.mini-runtime";

    return "/root/.mini-runtime";
}

const std::string RUNTIME_DIR = getRuntimeDir();

std::string lockPath(const std::string& name) {
    return RUNTIME_DIR + "/locks/" + name + ".lock";
}

std::string containerDir(const std::string& name) {
    return RUNTIME_DIR + "/containers/" + name;
}

bool validName(const std::string& name) {
    return !name.empty() && name.size() <= 64 &&
        name.find_first_not_of(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        ) == std::string::npos;
}

// Converts values such as 512K, 100M or 1G to bytes.
// A suffix is required so that "100" is not silently read as 100 bytes.
bool parseMemory(const std::string& value, unsigned long long& bytes) {
    if (value.size() < 2)
        return false;

    std::string number = value.substr(0, value.size() - 1);
    char suffix = static_cast<char>(
        std::toupper(static_cast<unsigned char>(value.back()))
    );

    if (number.find_first_not_of("0123456789") != std::string::npos)
        return false;

    unsigned long long multiplier;
    if (suffix == 'K')
        multiplier = 1024ULL;
    else if (suffix == 'M')
        multiplier = 1024ULL * 1024;
    else if (suffix == 'G')
        multiplier = 1024ULL * 1024 * 1024;
    else
        return false;

    try {
        unsigned long long amount = std::stoull(number);
        if (amount == 0 || amount > 1024ULL * 1024 * 1024)
            return false;
        bytes = amount * multiplier;
    } catch (...) {
        return false;
    }

    // Keep values within a reasonable range for this project:
    // below 4 MB even /bin/sh cannot start, above 1 TB is a typo.
    return bytes >= 4ULL * 1024 * 1024 &&
           bytes <= 1024ULL * 1024 * 1024 * 1024;
}

bool parseInt(const std::string& value, int low, int high, int& out) {
    try {
        std::size_t used = 0;
        int number = std::stoi(value, &used);

        if (used != value.size() || number < low || number > high)
            return false;

        out = number;
        return true;
    } catch (...) {
        return false;
    }
}

// Parses "[--memory 100M] [--cpu 50] [--pids 64] [--image base] [--net]
// [-- command args...]" starting at argv[first].
bool parseRunOptions(int argc, char* argv[], int first, RunOptions& options) {
    for (int i = first; i < argc; ) {
        std::string option = argv[i++];

        if (option == "--") {
            if (i >= argc) {
                std::cerr << "Missing command after --\n";
                return false;
            }

            options.command.assign(argv + i, argv + argc);
            return true;
        }

        if (option == "--net") {
            options.network = true;
            continue;
        }

        if (i >= argc) {
            std::cerr << "Missing value for " << option << "\n";
            return false;
        }

        std::string value = argv[i++];

        if (option == "--memory") {
            unsigned long long bytes = 0;
            if (!parseMemory(value, bytes)) {
                std::cerr << "Invalid memory limit: " << value
                          << ". Use a value from 4M to 1024G, such as 100M"
                          << " or 1G (the suffix K, M or G is required).\n";
                return false;
            }
            options.limits.memory = value;
        } else if (option == "--cpu") {
            if (!parseInt(value, 1, 100, options.limits.cpuPercent)) {
                std::cerr << "CPU must be a whole number from 1 to 100.\n";
                return false;
            }
        } else if (option == "--pids") {
            if (!parseInt(value, 1, 32768, options.limits.pids)) {
                std::cerr << "PIDs must be a whole number from 1 to 32768.\n";
                return false;
            }
        } else if (option == "--image") {
            options.image = value;
        } else {
            std::cerr << "Unknown option: " << option << "\n";
            return false;
        }
    }

    return true;
}

bool writeFile(const std::string& path, const std::string& value,
               bool quiet = false) {
    std::ofstream file(path);

    if (!file) {
        if (!quiet)
            std::cerr << "Failed to open " << path
                      << ": " << std::strerror(errno) << "\n";
        return false;
    }

    file << value;
    file.close();

    if (!file) {
        if (!quiet)
            std::cerr << "Failed to write " << path << "\n";
        return false;
    }

    return true;
}

std::string readFile(const std::string& path) {
    std::ifstream file(path);
    std::stringstream buffer;
    buffer << file.rdbuf();
    return buffer.str();
}

std::string trim(const std::string& value) {
    std::size_t start = value.find_first_not_of(" \t\r\n");
    if (start == std::string::npos)
        return "";
    std::size_t end = value.find_last_not_of(" \t\r\n");
    return value.substr(start, end - start + 1);
}

// Returns the value of "key value" in files like memory.events or cpu.stat.
long long readStat(const std::string& path, const std::string& key) {
    std::ifstream file(path);
    std::string name;
    long long value;

    while (file >> name >> value) {
        if (name == key)
            return value;
    }

    return -1;
}

bool makeDirectory(const std::string& path) {
    if (mkdir(path.c_str(), 0755) == 0)
        return true;

    if (errno == EEXIST) {
        struct stat info {};
        if (stat(path.c_str(), &info) == 0 && S_ISDIR(info.st_mode))
            return true;
    }

    std::cerr << "Failed to create directory " << path
              << ": " << std::strerror(errno) << "\n";
    return false;
}

// --------------------------------------------------------------------
// Images
// --------------------------------------------------------------------

// "base" means ~/.mini-runtime/images/base; anything with a "/" is a path.
std::string resolveImage(const std::string& image) {
    std::string path = image.find('/') == std::string::npos
        ? RUNTIME_DIR + "/images/" + image
        : image;

    std::error_code ec;
    fs::path absolute = fs::canonical(path, ec);
    return ec ? path : absolute.string();
}

bool validImageDir(const std::string& path) {
    // OverlayFS uses "," and ":" as option separators.
    if (path.find_first_of(",:") != std::string::npos) {
        std::cerr << "Image path must not contain ',' or ':': "
                  << path << "\n";
        return false;
    }

    std::error_code ec;
    if (!fs::is_directory(path, ec)) {
        std::cerr << "Image not found: " << path << "\n"
                  << "Run 'make setup' to build the base image, or pass a "
                  << "directory that contains a root filesystem.\n";
        return false;
    }

    return true;
}

std::string storedImage(const std::string& name) {
    return trim(readFile(containerDir(name) + "/image"));
}

// Creates the container directories and records which image it uses.
// Running an existing container with a different image is refused, because
// its upper layer was built against the old one.
bool prepareContainer(const std::string& name, const std::string& image,
                      bool mustBeNew) {
    const std::string dir = containerDir(name);
    std::error_code ec;
    bool exists = fs::is_directory(dir, ec);

    if (exists && mustBeNew) {
        std::cerr << "Container already exists: " << name << "\n";
        return false;
    }

    std::string current = exists ? storedImage(name) : "";
    std::string wanted = image.empty()
        ? (current.empty() ? resolveImage("base") : current)
        : resolveImage(image);

    if (!current.empty() && current != wanted) {
        std::cerr << "Container '" << name << "' uses image " << current
                  << ".\nDelete it first to switch to " << wanted << ".\n";
        return false;
    }

    if (!validImageDir(wanted))
        return false;

    if (!makeDirectory(RUNTIME_DIR + "/containers") ||
        !makeDirectory(dir) ||
        !makeDirectory(dir + "/upper") ||
        !makeDirectory(dir + "/work") ||
        !makeDirectory(dir + "/merged"))
        return false;

    return current == wanted || writeFile(dir + "/image", wanted + "\n");
}

int createContainer(int argc, char* argv[]) {
    const std::string name = argv[2];

    if (!validName(name)) {
        std::cerr << "Invalid container name. Use letters, numbers, _ or -.\n";
        return 1;
    }

    std::string image;
    if (argc == 5 && std::string(argv[3]) == "--image") {
        image = argv[4];
    } else if (argc != 3) {
        std::cerr << "Usage: sudo ./mini-runtime create <name> "
                  << "[--image <name-or-dir>]\n";
        return 1;
    }

    if (!prepareContainer(name, image, true))
        return 1;

    std::cout << "Created container: " << name
              << " (image " << storedImage(name) << ")\n";
    return 0;
}

// --------------------------------------------------------------------
// list / delete / diff
// --------------------------------------------------------------------

enum class State { Running, Stopped, Unknown };

State containerState(const std::string& name) {
    const std::string path = lockPath(name);

    // Lock files are world-readable, so "list" also works without sudo.
    int fd = open(path.c_str(), O_RDONLY);
    if (fd == -1)
        return errno == ENOENT ? State::Stopped : State::Unknown;

    if (flock(fd, LOCK_EX | LOCK_NB) == -1) {
        bool running = errno == EWOULDBLOCK || errno == EAGAIN;
        close(fd);
        return running ? State::Running : State::Unknown;
    }

    flock(fd, LOCK_UN);
    close(fd);
    return State::Stopped;
}

int listContainers() {
    const std::string path = RUNTIME_DIR + "/containers";
    std::error_code ec;

    if (!fs::exists(path, ec)) {
        std::cout << "No containers found.\n";
        return 0;
    }

    if (ec || !fs::is_directory(path, ec)) {
        std::cerr << "Cannot read container directory: " << path << "\n";
        return 1;
    }

    bool found = false;
    std::cout << "NAME\tSTATUS\tIMAGE\n";

    for (const auto& entry : fs::directory_iterator(path, ec)) {
        if (!entry.is_directory(ec) || ec) {
            ec.clear();
            continue;
        }

        std::string name = entry.path().filename().string();
        std::string image = storedImage(name);
        State state = containerState(name);

        std::cout << name << "\t"
                  << (state == State::Running ? "RUNNING" :
                      state == State::Stopped ? "STOPPED" : "UNKNOWN")
                  << "\t" << (image.empty() ? "-" : image) << "\n";
        found = true;
    }

    if (ec) {
        std::cerr << "Failed to list containers: " << ec.message() << "\n";
        return 1;
    }

    if (!found)
        std::cout << "No containers found.\n";

    return 0;
}

int deleteContainer(const std::string& name) {
    if (!validName(name)) {
        std::cerr << "Invalid container name.\n";
        return 1;
    }

    const std::string dir = containerDir(name);
    std::error_code ec;

    if (!fs::exists(dir, ec) || ec || !fs::is_directory(dir, ec)) {
        std::cerr << "Container not found: " << name << "\n";
        return 1;
    }

    if (!makeDirectory(RUNTIME_DIR + "/locks"))
        return 1;

    int fd = open(lockPath(name).c_str(), O_CREAT | O_RDWR, 0644);
    if (fd == -1) {
        std::cerr << "Cannot open container lock: "
                  << std::strerror(errno) << "\n";
        return 1;
    }

    if (flock(fd, LOCK_EX | LOCK_NB) == -1) {
        std::cerr << "Cannot delete '" << name
                  << "': container is running or its lock is unavailable.\n";
        close(fd);
        return 1;
    }

    // Older versions leaked these mounts to the host; detach them if present.
    umount2((dir + "/merged/proc").c_str(), MNT_DETACH);
    umount2((dir + "/merged").c_str(), MNT_DETACH);

    fs::remove_all(dir, ec);

    if (ec) {
        std::cerr << "Failed to delete container: " << ec.message() << "\n";
        flock(fd, LOCK_UN);
        close(fd);
        return 1;
    }

    unlink(lockPath(name).c_str());
    std::cout << "Deleted container: " << name << "\n";
    flock(fd, LOCK_UN);
    close(fd);
    return 0;
}

// Shows what the container changed compared with its image, by reading the
// OverlayFS upper layer. Deleted files appear as 0/0 character devices.
int diffContainer(const std::string& name) {
    if (!validName(name)) {
        std::cerr << "Invalid container name.\n";
        return 1;
    }

    const std::string upper = containerDir(name) + "/upper";
    const std::string lower = storedImage(name);
    std::error_code ec;

    if (!fs::is_directory(upper, ec)) {
        std::cerr << "Container not found: " << name << "\n";
        return 1;
    }

    int changes = 0;
    auto it = fs::recursive_directory_iterator(
        upper, fs::directory_options::skip_permission_denied, ec);

    for (; !ec && it != fs::recursive_directory_iterator(); it.increment(ec)) {
        const fs::path relative = fs::relative(it->path(), upper);
        const std::string inside = "/" + relative.string();
        struct stat info {};

        if (lstat(it->path().c_str(), &info) == -1)
            continue;

        bool inLower = !lower.empty() &&
            fs::exists(fs::symlink_status(fs::path(lower) / relative));

        if (S_ISCHR(info.st_mode) && info.st_rdev == makedev(0, 0)) {
            std::cout << "[DELETED]  " << inside << "\n";
        } else if (S_ISDIR(info.st_mode)) {
            if (inLower)
                continue;               // only a parent of a changed file
            std::cout << "[ADDED]    " << inside << "/\n";
        } else {
            std::cout << (inLower ? "[MODIFIED] " : "[ADDED]    ")
                      << inside << "\n";
        }

        ++changes;
    }

    if (changes == 0)
        std::cout << "No changes: the container still matches its image.\n";

    return 0;
}

// --------------------------------------------------------------------
// Container process
// --------------------------------------------------------------------

struct ChildArgs {
    std::string name;
    std::string image;
    std::vector<std::string> command;
    int gateRead;
    int gateWrite;
};

int pivotRoot(const char* newRoot, const char* putOld) {
    return static_cast<int>(syscall(SYS_pivot_root, newRoot, putOld));
}

bool mountDev() {
    if (mount("tmpfs", "/dev", "tmpfs", MS_NOSUID | MS_STRICTATIME,
              "mode=755,size=65536k") == -1) {
        perror("mount /dev");
        return false;
    }

    struct Device { const char* path; unsigned major; unsigned minor; };
    const Device devices[] = {
        {"/dev/null", 1, 3}, {"/dev/zero", 1, 5}, {"/dev/full", 1, 7},
        {"/dev/random", 1, 8}, {"/dev/urandom", 1, 9}, {"/dev/tty", 5, 0},
    };

    for (const Device& device : devices) {
        if (mknod(device.path, S_IFCHR | 0666,
                  makedev(device.major, device.minor)) == -1) {
            perror(device.path);
            return false;
        }
        chmod(device.path, 0666);       // undo the umask
    }

    const char* links[][2] = {
        {"/proc/self/fd", "/dev/fd"},
        {"/proc/self/fd/0", "/dev/stdin"},
        {"/proc/self/fd/1", "/dev/stdout"},
        {"/proc/self/fd/2", "/dev/stderr"},
    };

    for (const auto& link : links) {
        if (symlink(link[0], link[1]) == -1)
            perror(link[1]);
    }

    return true;
}

// A new network namespace starts with loopback down.
void bringUpLoopback() {
    int fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd == -1)
        return;

    struct ifreq request {};
    std::strncpy(request.ifr_name, "lo", IFNAMSIZ - 1);

    if (ioctl(fd, SIOCGIFFLAGS, &request) == 0) {
        request.ifr_flags |= IFF_UP | IFF_RUNNING;
        if (ioctl(fd, SIOCSIFFLAGS, &request) == -1)
            perror("loopback up");
    }

    close(fd);
}

// Keeps only the capabilities Docker grants by default (minus CAP_MKNOD),
// so root inside the container cannot load modules, change the clock,
// reboot, remount filesystems and so on. Takes effect at execvp().
bool dropCapabilities() {
    const int keep[] = {
        CAP_CHOWN, CAP_DAC_OVERRIDE, CAP_FSETID, CAP_FOWNER,
        CAP_NET_RAW, CAP_SETGID, CAP_SETUID, CAP_SETFCAP, CAP_SETPCAP,
        CAP_NET_BIND_SERVICE, CAP_SYS_CHROOT, CAP_KILL, CAP_AUDIT_WRITE,
    };

    int last = std::atoi(readFile("/proc/sys/kernel/cap_last_cap").c_str());
    if (last <= 0)
        last = CAP_LAST_CAP;

    for (int cap = 0; cap <= last; ++cap) {
        bool kept = false;
        for (int k : keep)
            kept = kept || k == cap;

        if (!kept && prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) == -1 &&
            errno != EINVAL) {
            perror("drop capability");
            return false;
        }
    }

    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == -1) {
        perror("no_new_privs");
        return false;
    }

    return true;
}

int childFunc(void* arg) {
    ChildArgs* args = static_cast<ChildArgs*>(arg);

    close(args->gateWrite);

    char signal = '\0';
    ssize_t count;

    do {
        count = read(args->gateRead, &signal, 1);
    } while (count == -1 && errno == EINTR);

    close(args->gateRead);

    if (count != 1 || signal != 'G') {
        std::cerr << "Container startup cancelled.\n";
        return 1;
    }

    // The parent has moved us into our cgroup by now. A cgroup namespace
    // makes that cgroup look like the root from inside the container.
    if (unshare(CLONE_NEWCGROUP) == -1)
        perror("unshare cgroup namespace");

    // Stop our mounts from propagating back to the host. On systemd hosts
    // "/" is a shared mount, and without this the overlay and /proc mounts
    // would stay visible (and mounted) on the host after the container exits.
    if (mount(nullptr, "/", nullptr, MS_REC | MS_PRIVATE, nullptr) == -1) {
        perror("make mounts private");
        return 1;
    }

    const std::string dir = containerDir(args->name);
    const std::string merged = dir + "/merged";
    const std::string upper = dir + "/upper";
    const std::string work = dir + "/work";

    const std::string options =
        "lowerdir=" + args->image + ",upperdir=" + upper + ",workdir=" + work;

    if (mount("overlay", merged.c_str(), "overlay", 0, options.c_str()) == -1) {
        perror("mount overlay");
        return 1;
    }

    std::cout << "OverlayFS mounted for " << args->name << "\n";

    for (const char* sub : {"/proc", "/sys", "/dev", "/.old_root"}) {
        if (!makeDirectory(merged + sub))
            return 1;
    }

    // pivot_root swaps the whole mount tree, so unlike chroot there is no
    // path back to the host filesystem once the old root is detached.
    const std::string oldRoot = merged + "/.old_root";

    if (pivotRoot(merged.c_str(), oldRoot.c_str()) == -1) {
        perror("pivot_root");
        return 1;
    }

    if (chdir("/") == -1) {
        perror("chdir");
        return 1;
    }

    if (umount2("/.old_root", MNT_DETACH) == -1) {
        perror("umount old root");
        return 1;
    }
    rmdir("/.old_root");

    if (mount("proc", "/proc", "proc",
              MS_NOSUID | MS_NODEV | MS_NOEXEC, nullptr) == -1) {
        perror("mount proc");
        return 1;
    }

    if (mount("sysfs", "/sys", "sysfs",
              MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, nullptr) == -1)
        perror("mount sysfs");

    if (!mountDev())
        return 1;

    if (sethostname(args->name.c_str(), args->name.size()) == -1)
        perror("sethostname");

    bringUpLoopback();

    if (!dropCapabilities())
        return 1;

    setenv("PATH", "/bin:/usr/bin:/sbin:/usr/sbin", 1);
    setenv("HOSTNAME", args->name.c_str(), 1);

    std::vector<char*> argv;
    for (std::string& part : args->command)
        argv.push_back(part.data());
    argv.push_back(nullptr);

    std::cout << "Entering container: " << args->name << "\n" << std::flush;
    execvp(argv[0], argv.data());

    std::cerr << "Cannot run " << args->command[0] << ": "
              << std::strerror(errno) << "\n";
    return 127;
}

// --------------------------------------------------------------------
// Host side: cgroups and networking
// --------------------------------------------------------------------

bool hasWord(const std::string& text, const std::string& word) {
    std::istringstream words(text);
    std::string item;
    while (words >> item) {
        if (item == word)
            return true;
    }
    return false;
}

// The runtime needs the unified cgroup v2 hierarchy with the cpu, memory
// and pids controllers available to child cgroups.
bool checkCgroupV2() {
    const std::string controllers =
        readFile(CGROUP_ROOT + "/cgroup.controllers");

    if (controllers.empty()) {
        std::cerr << "cgroup v2 is not mounted at " << CGROUP_ROOT << ".\n"
                  << "This runtime needs a host booted with the unified "
                  << "cgroup hierarchy (the default on Ubuntu 22.04+, "
                  << "Fedora 31+ and Debian 11+).\n";
        return false;
    }

    for (const char* controller : {"cpu", "memory", "pids"}) {
        if (!hasWord(controllers, controller)) {
            std::cerr << "cgroup v2 controller '" << controller
                      << "' is not available on this host.\n";
            return false;
        }

        writeFile(CGROUP_ROOT + "/cgroup.subtree_control",
                  std::string("+") + controller, true);
    }

    const std::string enabled =
        readFile(CGROUP_ROOT + "/cgroup.subtree_control");

    for (const char* controller : {"cpu", "memory", "pids"}) {
        if (!hasWord(enabled, controller)) {
            std::cerr << "Could not enable the '" << controller
                      << "' controller in " << CGROUP_ROOT
                      << "/cgroup.subtree_control.\n";
            return false;
        }
    }

    return true;
}

// Runs a host command without a shell and returns true if it succeeded.
bool runHost(const std::vector<std::string>& command) {
    pid_t pid = fork();

    if (pid == -1) {
        perror("fork");
        return false;
    }

    if (pid == 0) {
        std::vector<char*> argv;
        for (const std::string& part : command)
            argv.push_back(const_cast<char*>(part.c_str()));
        argv.push_back(nullptr);

        execvp(argv[0], argv.data());
        std::cerr << "Cannot run " << command[0] << ": "
                  << std::strerror(errno) << "\n";
        _exit(127);
    }

    int status = 0;
    if (waitpid(pid, &status, 0) == -1)
        return false;

    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

// Connects the container to the host with a veth pair:
//   host  mr<pid>h  10.200.N.1/24  <---->  eth0  10.200.N.2/24  container
// The pair disappears automatically when the container's namespace dies.
bool setupNetwork(int pid, std::string& hostIp, std::string& containerIp) {
    const std::string target = std::to_string(pid);
    const std::string hostSide = "mr" + target + "h";
    const std::string peerSide = "mr" + target + "c";
    const std::string subnet = "10.200." + std::to_string(pid % 254 + 1);

    hostIp = subnet + ".1";
    containerIp = subnet + ".2";

    auto inside = [&](std::vector<std::string> command) {
        command.insert(command.begin(), {"nsenter", "-t", target, "-n"});
        return runHost(command);
    };

    return runHost({"ip", "link", "add", hostSide, "type", "veth",
                    "peer", "name", peerSide}) &&
           runHost({"ip", "link", "set", peerSide, "netns", target}) &&
           runHost({"ip", "addr", "add", hostIp + "/24", "dev", hostSide}) &&
           runHost({"ip", "link", "set", hostSide, "up"}) &&
           inside({"ip", "link", "set", peerSide, "name", "eth0"}) &&
           inside({"ip", "addr", "add", containerIp + "/24", "dev", "eth0"}) &&
           inside({"ip", "link", "set", "eth0", "up"}) &&
           inside({"ip", "route", "add", "default", "via", hostIp});
}

// Removes a cgroup left behind by a crashed run, if it is empty.
bool createCgroup(const std::string& cgroup) {
    if (mkdir(cgroup.c_str(), 0755) == 0)
        return true;

    if (errno == EEXIST && rmdir(cgroup.c_str()) == 0 &&
        mkdir(cgroup.c_str(), 0755) == 0)
        return true;

    std::cerr << "Cannot create cgroup " << cgroup << ": "
              << std::strerror(errno) << "\n";
    return false;
}

void printUsage() {
    std::cerr
        << "Usage:\n"
        << "  sudo ./mini-runtime create <name> [--image <name-or-dir>]\n"
        << "  sudo ./mini-runtime run <name> [--image <name-or-dir>]"
           " [--memory 100M] [--cpu 50] [--pids 64] [--net]"
           " [-- command args...]\n"
        << "  ./mini-runtime list\n"
        << "  ./mini-runtime diff <name>\n"
        << "  sudo ./mini-runtime delete <name>\n";
}

int runContainer(int argc, char* argv[]) {
    const std::string name = argv[2];

    if (!validName(name)) {
        std::cerr << "Invalid container name. Use letters, numbers, _ or -.\n";
        return 1;
    }

    RunOptions options;

    if (!parseRunOptions(argc, argv, 3, options)) {
        printUsage();
        return 1;
    }

    if (geteuid() != 0) {
        std::cerr << "run needs root: use sudo ./mini-runtime run ...\n";
        return 1;
    }

    if (!checkCgroupV2())
        return 1;

    if (!makeDirectory(RUNTIME_DIR) ||
        !makeDirectory(RUNTIME_DIR + "/containers") ||
        !makeDirectory(RUNTIME_DIR + "/locks"))
        return 1;

    int lockFd = open(lockPath(name).c_str(), O_CREAT | O_RDWR, 0644);
    if (lockFd == -1) {
        std::cerr << "Cannot open container lock: "
                  << std::strerror(errno) << "\n";
        return 1;
    }
    fchmod(lockFd, 0644);

    if (flock(lockFd, LOCK_EX | LOCK_NB) == -1) {
        std::cerr << "Container '" << name
                  << "' is already running or its lock is unavailable.\n";
        close(lockFd);
        return 1;
    }

    auto unlock = [&]() {
        flock(lockFd, LOCK_UN);
        close(lockFd);
    };

    if (!prepareContainer(name, options.image, false)) {
        unlock();
        return 1;
    }

    char* stack = new (std::nothrow) char[STACK_SIZE];
    if (!stack) {
        std::cerr << "Failed to allocate child stack\n";
        unlock();
        return 1;
    }

    int gate[2];
    if (pipe(gate) == -1) {
        perror("pipe");
        delete[] stack;
        unlock();
        return 1;
    }

    ChildArgs args{name, storedImage(name), options.command, gate[0], gate[1]};

    const int flags =
        CLONE_NEWPID | CLONE_NEWNS | CLONE_NEWUTS |
        CLONE_NEWIPC | CLONE_NEWNET | SIGCHLD;

    int pid = clone(childFunc, stack + STACK_SIZE, flags, &args);
    if (pid == -1) {
        perror("clone");
        close(gate[0]);
        close(gate[1]);
        delete[] stack;
        unlock();
        return 1;
    }

    close(gate[0]);

    // Named after the container so tools (and a future GUI) can find it.
    const std::string cgroup = CGROUP_ROOT + "/mini-runtime-" + name;

    bool created = createCgroup(cgroup);
    bool ok = created;

    const std::string cpuQuota =
        std::to_string(options.limits.cpuPercent * 1000) + " 100000";
    unsigned long long memoryBytes = 0;
    parseMemory(options.limits.memory, memoryBytes);

    if (ok && !writeFile(cgroup + "/memory.max", std::to_string(memoryBytes)))
        ok = false;
    if (ok && !writeFile(cgroup + "/memory.swap.max", "0"))
        ok = false;
    if (ok && !writeFile(cgroup + "/pids.max",
                         std::to_string(options.limits.pids)))
        ok = false;
    if (ok && !writeFile(cgroup + "/cpu.max", cpuQuota))
        ok = false;
    if (ok && !writeFile(cgroup + "/cgroup.procs", std::to_string(pid)))
        ok = false;

    std::string hostIp, containerIp;
    if (ok && options.network && !setupNetwork(pid, hostIp, containerIp)) {
        std::cerr << "Network setup failed (needs the 'ip' and 'nsenter' "
                  << "commands on the host).\n";
        ok = false;
    }

    if (!ok) {
        std::cerr << "Container setup failed; cancelling startup.\n";
        close(gate[1]);
        kill(pid, SIGKILL);
        waitpid(pid, nullptr, 0);

        if (created && rmdir(cgroup.c_str()) == -1)
            std::cerr << "Warning: could not remove cgroup "
                      << cgroup << "\n";

        delete[] stack;
        unlock();
        return 1;
    }

    std::cout << "Container: " << name << "\n";
    std::cout << "Image: " << args.image << "\n";
    std::cout << "Host PID: " << pid << "\n";
    std::cout << "Memory limit: " << options.limits.memory << "\n";
    std::cout << "CPU limit: " << options.limits.cpuPercent << "%\n";
    std::cout << "PID limit: " << options.limits.pids << "\n";
    if (options.network)
        std::cout << "Network: container " << containerIp
                  << ", host " << hostIp << "\n";
    std::cout << "Starting: " << options.command[0] << "\n" << std::flush;

    // Ctrl-C and Ctrl-\ belong to the container; the runtime must survive
    // them so it can clean up the cgroup afterwards.
    signal(SIGINT, SIG_IGN);
    signal(SIGQUIT, SIG_IGN);

    const char startSignal = 'G';
    ssize_t written;

    do {
        written = write(gate[1], &startSignal, 1);
    } while (written == -1 && errno == EINTR);

    close(gate[1]);

    if (written != 1) {
        perror("release child");
        kill(pid, SIGKILL);
        waitpid(pid, nullptr, 0);

        if (rmdir(cgroup.c_str()) == -1)
            std::cerr << "Warning: could not remove cgroup\n";

        delete[] stack;
        unlock();
        return 1;
    }

    int status = 0;
    int waited;
    do {
        waited = waitpid(pid, &status, 0);
    } while (waited == -1 && errno == EINTR);

    if (waited == -1)
        perror("waitpid");

    // Report what the container used before its cgroup is removed.
    long long oomKills = readStat(cgroup + "/memory.events", "oom_kill");
    long long cpuUsec = readStat(cgroup + "/cpu.stat", "usage_usec");
    long long throttled = readStat(cgroup + "/cpu.stat", "nr_throttled");
    std::string peak = trim(readFile(cgroup + "/memory.peak"));

    std::cout << "\nResource usage: CPU " << (cpuUsec < 0 ? 0 : cpuUsec / 1000)
              << " ms, throttled " << (throttled < 0 ? 0 : throttled)
              << " times";
    if (!peak.empty())
        std::cout << ", peak memory "
                  << std::stoll(peak) / (1024 * 1024) << " MB";
    std::cout << "\n";

    if (oomKills > 0)
        std::cout << "Memory limit reached: the kernel OOM killer stopped "
                  << oomKills << " process(es).\n";

    // The kernel may need a moment to finish tearing down the processes.
    int removed = -1;
    for (int attempt = 0; attempt < 50 && removed == -1; ++attempt) {
        removed = rmdir(cgroup.c_str());
        if (removed == -1 && errno == EBUSY)
            usleep(20000);
        else
            break;
    }

    if (removed == -1) {
        std::cerr << "Warning: could not remove cgroup "
                  << cgroup << ": " << std::strerror(errno) << "\n";
    }

    delete[] stack;
    unlock();

    int exitCode = 1;
    if (WIFEXITED(status)) {
        exitCode = WEXITSTATUS(status);
        std::cout << "Container exited with code " << exitCode << ".\n";
    } else if (WIFSIGNALED(status)) {
        exitCode = 128 + WTERMSIG(status);
        std::cout << "Container was killed by signal " << WTERMSIG(status)
                  << " (" << strsignal(WTERMSIG(status)) << ").\n";
    }

    return exitCode;
}

int main(int argc, char* argv[]) {
    const std::string command = argc > 1 ? argv[1] : "";

    if (argc == 2 && command == "list")
        return listContainers();

    if (argc == 3 && command == "delete")
        return deleteContainer(argv[2]);

    if (argc == 3 && command == "diff")
        return diffContainer(argv[2]);

    if (argc >= 3 && command == "create")
        return createContainer(argc, argv);

    if (argc >= 3 && command == "run")
        return runContainer(argc, argv);

    printUsage();
    return 1;
}

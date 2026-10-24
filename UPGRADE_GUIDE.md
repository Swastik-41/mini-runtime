# Mini-Runtime Upgrade Guide

> **Status (2026-10-12):** Parts 1, 2, 3, 4, 5, 7, 8 and 9 are now implemented in
> `src/main.cpp`, so their "find this block" snippets no longer match the code.
> Still open: Part 6 (user namespace / rootless) and Part 10 (lifecycle logging).

Each part below is **independent** — pick any one, apply it, commit, push.
Every part shows exactly **what to find** and **what to replace it with**.

---

## Part 1 — PID Limit (`pids.max`)

**Time:** ~15 min
**Concept:** cgroups can limit the number of processes. Prevents fork bombs.

### Step 1: Edit `src/main.cpp`

Find this block (around line 249–254):

```cpp
    if (!writeFile(
            cgroup + "/cpu.max",
            "50000 100000")) {

        std::cerr << "Warning: failed to set cpu.max\n";
    }
```

Right **after** it, paste:

```cpp

    // --------------------------------------------------------
    // PID limit: max 64 processes
    //
    // Prevents fork bombs from escaping the container.
    // --------------------------------------------------------
    if (!writeFile(
            cgroup + "/pids.max",
            "64")) {

        std::cerr << "Warning: failed to set pids.max\n";
    }
```

Find this line (around line 267):

```cpp
    std::cout << "CPU limit: 50%\n";
```

Right **after** it, paste:

```cpp
    std::cout << "PID limit: 64\n";
```

### Step 2: Update README.md

Find:

```
   - CPU limit: 50%
   - Demonstrates kernel-enforced OOM termination
```

Replace with:

```
   - CPU limit: 50%
   - PID limit: 64 processes (fork bomb protection)
   - Demonstrates kernel-enforced OOM termination
```

### Step 3: Test

```bash
make
sudo ./mini-runtime
# Inside the container, try a fork bomb:
:(){ :|:& };:
# It should be killed / stopped. The host is safe.
```

### Step 4: Commit

```bash
git add src/main.cpp README.md
git commit -m "feat: add PID limit via cgroups pids.max

Limits container to 64 processes using cgroups v2 pids.max.
Prevents fork bombs from escaping the container."
git push
```

---

## Part 2 — Overlay Diff Viewer

**Time:** ~30 min
**Concept:** After the container exits, show which files were written to the upper layer. Makes copy-on-write **visible**.

### Step 1: Add include in `src/main.cpp`

Find:

```cpp
#include <pwd.h>
```

Right **after** it, paste:

```cpp
#include <ftw.h>
#include <vector>
```

### Step 2: Add the diff function

Find:

```cpp
constexpr int STACK_SIZE = 1024 * 1024;
```

Right **after** it, paste:

```cpp

// ------------------------------------------------------------
// Overlay diff: collect files modified in the upper layer
// ------------------------------------------------------------
static std::vector<std::string> g_diffFiles;
static std::string g_upperPrefix;

int diffCallback(const char* fpath, const struct stat* sb,
                 int typeflag, struct FTW* ftwbuf) {
    (void)sb; (void)ftwbuf;

    if (typeflag == FTW_F) {
        std::string relative = std::string(fpath).substr(g_upperPrefix.size());
        g_diffFiles.push_back(relative);
    }

    return 0;
}

void showOverlayDiff(const std::string& upperdir) {
    g_diffFiles.clear();
    g_upperPrefix = upperdir;

    nftw(upperdir.c_str(), diffCallback, 20, FTW_PHYS);

    if (g_diffFiles.empty()) {
        std::cout << "\n[COW] No files were modified in the container.\n";
    } else {
        std::cout << "\n[COW] Files modified in upper layer (copy-on-write):\n";

        for (const auto& f : g_diffFiles) {
            std::cout << "  [COW] " << f << "\n";
        }
    }
}
```

### Step 3: Store the upperdir path and call the diff

Find this block (around line 278–285):

```cpp
    // --------------------------------------------------------
    // Remove cgroup
    // --------------------------------------------------------
    if (rmdir(cgroup.c_str()) == -1) {
```

Right **before** it, paste:

```cpp
    // --------------------------------------------------------
    // Show overlay diff (copy-on-write changes)
    // --------------------------------------------------------
    std::string upperdir =
        RUNTIME_DIR + "/containers/demo/upper";
    showOverlayDiff(upperdir);

```

### Step 4: Update README.md

Find:

```
   - Merged container root filesystem
```

Replace with:

```
   - Merged container root filesystem
   - Diff viewer: shows which files were modified via COW on exit
```

### Step 5: Test

```bash
make
sudo ./mini-runtime
# Inside the container:
echo "hello" > /tmp/testfile.txt
echo "modified" > /message.txt
exit
# You should see:
#   [COW] /tmp/testfile.txt
#   [COW] /message.txt
```

### Step 6: Commit

```bash
git add src/main.cpp README.md
git commit -m "feat: show overlay diff on container exit

Walks the upper layer after the container exits and prints
all files that were created or modified, making the
copy-on-write behavior visible."
git push
```

---

## Part 3 — `pivot_root` Instead of `chroot`

**Time:** ~30–45 min
**Concept:** `chroot` is escapable. `pivot_root` is what real container runtimes use — it fully detaches the old root.

### Step 1: Add include in `src/main.cpp`

Find:

```cpp
#include <sys/utsname.h>
```

Right **after** it, paste:

```cpp
#include <sys/syscall.h>
```

### Step 2: Replace the chroot block in `childFunc`

Find this entire block:

```cpp
    // --------------------------------------------------------
    // Change root filesystem
    // --------------------------------------------------------
    if (chroot(merged.c_str()) == -1) {
        perror("chroot");
        return 1;
    }

    if (chdir("/") == -1) {
        perror("chdir");
        return 1;
    }
```

Replace with:

```cpp
    // --------------------------------------------------------
    // pivot_root: properly isolate the root filesystem
    //
    // Unlike chroot, pivot_root fully detaches the old root.
    // This prevents container escape via chroot tricks.
    // --------------------------------------------------------

    // pivot_root requires the new root to be a mount point
    if (mount(merged.c_str(),
              merged.c_str(),
              nullptr,
              MS_BIND | MS_REC,
              nullptr) == -1) {

        perror("bind mount new root");
        return 1;
    }

    // Create directory to stash the old root
    std::string oldRoot = merged + "/old_root";
    mkdir(oldRoot.c_str(), 0755);

    // pivot_root: swap old and new root
    if (syscall(SYS_pivot_root,
                merged.c_str(),
                oldRoot.c_str()) == -1) {

        perror("pivot_root");
        return 1;
    }

    if (chdir("/") == -1) {
        perror("chdir");
        return 1;
    }

    // Unmount the old root — it's no longer needed
    if (umount2("/old_root", MNT_DETACH) == -1) {
        perror("umount old root");
    }

    rmdir("/old_root");
```

### Step 3: Update README.md

Find:

```
Change root filesystem
```

Replace with:

```
pivot_root (secure root isolation)
```

Also find:

```
   - Network namespace
```

After it, add:

```
   - Secure root isolation via `pivot_root` (not `chroot`)
```

### Step 4: Test

```bash
make
sudo ./mini-runtime
# Inside the container:
ls /
# You should NOT see /old_root — it was unmounted
# The container root is properly isolated
exit
```

### Step 5: Commit

```bash
git add src/main.cpp README.md
git commit -m "feat: replace chroot with pivot_root for proper root isolation

pivot_root fully detaches the old root filesystem, preventing
the well-known chroot escape techniques. This matches the
approach used by production container runtimes."
git push
```

---

## Part 4 — Configurable Resource Limits via CLI

**Time:** ~45 min
**Concept:** Pass `--memory 256M --cpu 25 --pids 100` instead of hardcoded values.

### Step 1: Add a config struct and argument parser in `src/main.cpp`

Find:

```cpp
constexpr int STACK_SIZE = 1024 * 1024;
```

Right **after** it, paste:

```cpp

// ------------------------------------------------------------
// Container configuration (parsed from CLI)
// ------------------------------------------------------------
struct ContainerConfig {
    std::string memory = "100M";
    std::string cpu    = "50000 100000";
    std::string pids   = "64";
    int cpuPercent     = 50;
};

ContainerConfig parseArgs(int argc, char* argv[]) {
    ContainerConfig cfg;

    for (int i = 1; i < argc; i++) {
        std::string arg = argv[i];

        if (arg == "--memory" && i + 1 < argc) {
            cfg.memory = argv[++i];
        }
        else if (arg == "--cpu" && i + 1 < argc) {
            cfg.cpuPercent = std::stoi(argv[++i]);
            int quota = cfg.cpuPercent * 1000;   // out of 100000
            cfg.cpu = std::to_string(quota) + " 100000";
        }
        else if (arg == "--pids" && i + 1 < argc) {
            cfg.pids = argv[++i];
        }
        else if (arg == "--help" || arg == "-h") {
            std::cout << "Usage: mini-runtime [options]\n"
                      << "  --memory <limit>   Memory limit (e.g. 100M, 1G)\n"
                      << "  --cpu <percent>    CPU limit (1-100)\n"
                      << "  --pids <max>       Max number of processes\n";
            exit(0);
        }
    }

    return cfg;
}
```

### Step 2: Update `main()` signature and use config

Find:

```cpp
int main() {
```

Replace with:

```cpp
int main(int argc, char* argv[]) {

    ContainerConfig cfg = parseArgs(argc, argv);
```

### Step 3: Replace hardcoded cgroup values

Find:

```cpp
    if (!writeFile(
            cgroup + "/memory.max",
            "100M")) {
```

Replace with:

```cpp
    if (!writeFile(
            cgroup + "/memory.max",
            cfg.memory)) {
```

Find:

```cpp
    if (!writeFile(
            cgroup + "/cpu.max",
            "50000 100000")) {
```

Replace with:

```cpp
    if (!writeFile(
            cgroup + "/cpu.max",
            cfg.cpu)) {
```

Find:

```cpp
    std::cout << "Memory limit: 100M\n";
    std::cout << "CPU limit: 50%\n";
```

Replace with:

```cpp
    std::cout << "Memory limit: " << cfg.memory << "\n";
    std::cout << "CPU limit: " << cfg.cpuPercent << "%\n";
```

*If you have Part 1 (pids.max) applied, also replace:*

```cpp
    if (!writeFile(
            cgroup + "/pids.max",
            "64")) {
```

with:

```cpp
    if (!writeFile(
            cgroup + "/pids.max",
            cfg.pids)) {
```

And replace:

```cpp
    std::cout << "PID limit: 64\n";
```

with:

```cpp
    std::cout << "PID limit: " << cfg.pids << "\n";
```

### Step 4: Update README.md

Add this section after the Architecture block:

```markdown

---

## Usage

```bash
# Default limits (100M memory, 50% CPU)
sudo ./mini-runtime

# Custom limits
sudo ./mini-runtime --memory 256M --cpu 75 --pids 128

# See all options
./mini-runtime --help
```
```

### Step 5: Test

```bash
make
sudo ./mini-runtime --memory 50M --cpu 25
# Inside the container, run memory_hog — it should OOM faster
```

### Step 6: Commit

```bash
git add src/main.cpp README.md
git commit -m "feat: configurable resource limits via CLI flags

Supports --memory, --cpu, and --pids flags.
Defaults to 100M memory, 50% CPU if not specified."
git push
```

---

## Part 5 — Proper `/dev` and `/sys` Mounting

**Time:** ~30 min
**Concept:** Containers need more than `/proc` to run real programs. Mount `/dev` and `/sys` too.

### Step 1: Edit `childFunc` in `src/main.cpp`

Find this block:

```cpp
    std::cout << "OverlayFS mounted successfully\n";

    // --------------------------------------------------------
    // Mount /proc inside the container
    // --------------------------------------------------------
    std::string proc = merged + "/proc";

    mkdir(proc.c_str(), 0755);

    if (mount("proc",
              proc.c_str(),
              "proc",
              0,
              nullptr) == -1) {

        perror("mount proc");
        umount(merged.c_str());
        return 1;
    }
```

Replace with:

```cpp
    std::cout << "OverlayFS mounted successfully\n";

    // --------------------------------------------------------
    // Mount /proc inside the container
    // --------------------------------------------------------
    std::string proc = merged + "/proc";

    mkdir(proc.c_str(), 0755);

    if (mount("proc",
              proc.c_str(),
              "proc",
              0,
              nullptr) == -1) {

        perror("mount proc");
        umount(merged.c_str());
        return 1;
    }

    // --------------------------------------------------------
    // Mount /sys inside the container (read-only)
    // --------------------------------------------------------
    std::string sys = merged + "/sys";
    mkdir(sys.c_str(), 0755);

    if (mount("sysfs",
              sys.c_str(),
              "sysfs",
              MS_RDONLY,
              nullptr) == -1) {

        perror("mount sysfs (non-fatal)");
    }

    // --------------------------------------------------------
    // Mount /dev inside the container
    //
    // Bind-mount essential device nodes so programs like
    // shell redirection (> /dev/null) work correctly.
    // --------------------------------------------------------
    std::string dev = merged + "/dev";
    mkdir(dev.c_str(), 0755);

    if (mount("tmpfs",
              dev.c_str(),
              "tmpfs",
              MS_NOSUID | MS_STRICTATIME,
              "mode=755,size=65536k") == -1) {

        perror("mount tmpfs on /dev (non-fatal)");
    } else {
        // Create essential device nodes
        const char* devNodes[] = {
            "/dev/null",
            "/dev/zero",
            "/dev/urandom",
            "/dev/random",
            "/dev/tty"
        };

        for (const char* node : devNodes) {
            std::string src = node;
            std::string dst = merged + node;

            // Create the target file for bind mount
            int fd = open(dst.c_str(),
                          O_WRONLY | O_CREAT, 0666);
            if (fd >= 0) close(fd);

            if (mount(src.c_str(),
                      dst.c_str(),
                      nullptr,
                      MS_BIND,
                      nullptr) == -1) {

                std::cerr << "Warning: bind mount "
                          << node << " failed\n";
            }
        }
    }
```

### Step 2: Add the missing include

Find:

```cpp
#include <cerrno>
```

Right **after** it, paste:

```cpp
#include <fcntl.h>
```

### Step 3: Update README.md

Find:

```
   - Network namespace
```

After it, add:

```
   - Proper `/proc`, `/sys`, and `/dev` mounting
```

### Step 4: Test

```bash
make
sudo ./mini-runtime
# Inside the container:
echo "test" > /dev/null    # should work now
cat /dev/urandom | head -c 16 | od -x    # random bytes
ls /sys/    # should list kernel subsystems
exit
```

### Step 5: Commit

```bash
git add src/main.cpp README.md
git commit -m "feat: mount /dev and /sys for a more functional container

Mounts sysfs read-only and creates a tmpfs /dev with
bind-mounted essential device nodes (null, zero, urandom,
random, tty). Programs inside the container can now use
standard device files."
git push
```

---

## Part 6 — User Namespace (Rootless Containers)

**Time:** ~1 hr
**Concept:** Run the container without `sudo`. The container process thinks it is root, but on the host it's your normal user.

### Step 1: Add includes in `src/main.cpp`

Find:

```cpp
#include <pwd.h>
```

Right **after** it, paste:

```cpp
#include <signal.h>
```

### Step 2: Add synchronization pipe and uid/gid mapping

This part needs the parent to write the UID/GID map before the child proceeds. We use a pipe for synchronization.

Find:

```cpp
int childFunc(void*) {
```

Replace with:

```cpp
// Pipe file descriptor — child reads from this to wait
// until the parent has written the UID/GID maps.
static int g_syncPipe[2];

int childFunc(void*) {

    // Wait for the parent to set up UID/GID mappings
    close(g_syncPipe[1]);  // close write end
    char ch;
    if (read(g_syncPipe[0], &ch, 1) != 0) {
        // parent signals by closing the pipe
    }
    close(g_syncPipe[0]);
```

### Step 3: Add `CLONE_NEWUSER` to the flags

Find:

```cpp
    int flags =
        CLONE_NEWPID |
        CLONE_NEWNS |
        CLONE_NEWUTS |
        CLONE_NEWIPC |
        CLONE_NEWNET |
        SIGCHLD;
```

Replace with:

```cpp
    int flags =
        CLONE_NEWUSER |
        CLONE_NEWPID |
        CLONE_NEWNS |
        CLONE_NEWUTS |
        CLONE_NEWIPC |
        CLONE_NEWNET |
        SIGCHLD;
```

### Step 4: Create the pipe and write mappings after clone

Find:

```cpp
    // --------------------------------------------------------
    // Create isolated process
    // --------------------------------------------------------
```

Right **before** it, paste:

```cpp
    // --------------------------------------------------------
    // Create sync pipe for user namespace setup
    // --------------------------------------------------------
    if (pipe(g_syncPipe) == -1) {
        perror("pipe");
        delete[] stack;
        return 1;
    }

```

Find:

```cpp
    std::cout << "Host PID: " << getpid() << "\n";
    std::cout << "Container PID: " << pid << "\n";
```

Replace with:

```cpp
    std::cout << "Host PID: " << getpid() << "\n";
    std::cout << "Container PID: " << pid << "\n";

    // --------------------------------------------------------
    // Write UID/GID mappings for user namespace
    //
    // Maps UID 0 inside the container to the real user outside.
    // This is how rootless containers work (Podman model).
    // --------------------------------------------------------
    uid_t realUid = getuid();
    gid_t realGid = getgid();

    // Must deny setgroups before writing gid_map
    writeFile("/proc/" + std::to_string(pid) + "/setgroups", "deny");
    writeFile("/proc/" + std::to_string(pid) + "/uid_map",
              "0 " + std::to_string(realUid) + " 1");
    writeFile("/proc/" + std::to_string(pid) + "/gid_map",
              "0 " + std::to_string(realGid) + " 1");

    // Signal the child that mappings are ready
    close(g_syncPipe[0]);  // close read end
    close(g_syncPipe[1]);  // closing write end wakes the child

    std::cout << "User namespace: UID " << realUid
              << " -> 0 (root inside container)\n";
```

### Step 5: Update README.md

Find:

```
   - Network namespace
```

After it, add:

```
   - User namespace (rootless — no `sudo` required)
```

### Step 6: Test

```bash
make
# Run WITHOUT sudo:
./mini-runtime
# Inside the container:
id
# Should show: uid=0(root) gid=0(root)
# But on the host, it's your normal user!
```

**Note:** Some operations (like mounting overlayfs) may still need kernel support for unprivileged overlayfs (`CONFIG_USER_NS_UNPRIVILEGED`). If overlayfs mount fails without sudo, you may need to run: `sudo sysctl -w kernel.unprivileged_userns_clone=1`

### Step 7: Commit

```bash
git add src/main.cpp README.md
git commit -m "feat: add user namespace for rootless container execution

Uses CLONE_NEWUSER with UID/GID mapping so the container
runs as root inside but maps to the real user on the host.
This is the rootless container model used by Podman."
git push
```

---

## Part 7 — CLI with Named Containers

**Time:** ~1–1.5 hrs
**Concept:** Create/list/delete named containers instead of a hardcoded "demo".

### Step 1: Add includes in `src/main.cpp`

Find:

```cpp
#include <pwd.h>
```

Right **after** it, paste:

```cpp
#include <dirent.h>
```

### Step 2: Add container name as a global and CLI commands

Find:

```cpp
constexpr int STACK_SIZE = 1024 * 1024;
```

Right **after** it, paste:

```cpp

// ------------------------------------------------------------
// Container name (set from CLI)
// ------------------------------------------------------------
static std::string g_containerName = "demo";

// ------------------------------------------------------------
// List existing containers
// ------------------------------------------------------------
void listContainers() {
    std::string path = getRuntimeDir() + "/containers";
    DIR* dir = opendir(path.c_str());

    if (!dir) {
        std::cout << "No containers found.\n";
        return;
    }

    std::cout << "Containers:\n";
    struct dirent* entry;
    bool found = false;

    while ((entry = readdir(dir)) != nullptr) {
        if (entry->d_name[0] == '.') continue;
        if (entry->d_type == DT_DIR) {
            std::cout << "  " << entry->d_name << "\n";
            found = true;
        }
    }

    closedir(dir);

    if (!found) {
        std::cout << "  (none)\n";
    }
}

// ------------------------------------------------------------
// Delete a container's overlay directories
// ------------------------------------------------------------
void deleteContainer(const std::string& name) {
    std::string base = getRuntimeDir() + "/containers/" + name;
    std::string cmd = "rm -rf " + base;

    if (system(cmd.c_str()) == 0) {
        std::cout << "Deleted container: " << name << "\n";
    } else {
        std::cerr << "Failed to delete container: " << name << "\n";
    }
}
```

### Step 3: Update `childFunc` to use the container name

Find all four occurrences of `"/containers/demo/"` in childFunc and replace with:

```cpp
"/containers/" + g_containerName + "/"
```

Specifically, replace these lines:

```cpp
    std::string merged =
        RUNTIME_DIR + "/containers/demo/merged";
```
→
```cpp
    std::string merged =
        RUNTIME_DIR + "/containers/" + g_containerName + "/merged";
```

```cpp
    std::string upperdir =
        RUNTIME_DIR + "/containers/demo/upper";
```
→
```cpp
    std::string upperdir =
        RUNTIME_DIR + "/containers/" + g_containerName + "/upper";
```

```cpp
    std::string workdir =
        RUNTIME_DIR + "/containers/demo/work";
```
→
```cpp
    std::string workdir =
        RUNTIME_DIR + "/containers/" + g_containerName + "/work";
```

### Step 4: Update `main()` to parse subcommands

Find:

```cpp
int main() {

    std::cout << "Mini Container Runtime\n";
    std::cout << "Runtime directory: "
              << RUNTIME_DIR << "\n";
```

Replace with:

```cpp
int main(int argc, char* argv[]) {

    if (argc >= 2) {
        std::string cmd = argv[1];

        if (cmd == "list") {
            listContainers();
            return 0;
        }

        if (cmd == "delete" && argc >= 3) {
            deleteContainer(argv[2]);
            return 0;
        }

        if (cmd == "run" && argc >= 3) {
            g_containerName = argv[2];
        } else if (cmd != "run") {
            std::cerr << "Usage:\n"
                      << "  mini-runtime run <name>\n"
                      << "  mini-runtime list\n"
                      << "  mini-runtime delete <name>\n";
            return 1;
        }
    }

    // Create container directories if they don't exist
    std::string containerBase =
        RUNTIME_DIR + "/containers/" + g_containerName;

    mkdir((containerBase + "/upper").c_str(), 0755);
    mkdir((containerBase + "/work").c_str(), 0755);
    mkdir((containerBase + "/merged").c_str(), 0755);

    std::cout << "Mini Container Runtime\n";
    std::cout << "Container: " << g_containerName << "\n";
    std::cout << "Runtime directory: "
              << RUNTIME_DIR << "\n";
```

### Step 5: Also fix the overlay diff path if you have Part 2

If you applied Part 2, find:

```cpp
    std::string upperdir =
        RUNTIME_DIR + "/containers/demo/upper";
    showOverlayDiff(upperdir);
```

Replace with:

```cpp
    std::string upperdir =
        RUNTIME_DIR + "/containers/" + g_containerName + "/upper";
    showOverlayDiff(upperdir);
```

### Step 6: Update README.md

Add a usage section:

```markdown

## Usage

```bash
# Run a container (creates it if it doesn't exist)
sudo ./mini-runtime run mycontainer

# Run with default name "demo"
sudo ./mini-runtime

# List all containers
sudo ./mini-runtime list

# Delete a container
sudo ./mini-runtime delete mycontainer
```
```

### Step 7: Test

```bash
make
sudo ./mini-runtime run test1
# modify a file, exit
sudo ./mini-runtime run test2
# different container, different upper layer
sudo ./mini-runtime list
# shows: test1, test2
sudo ./mini-runtime delete test1
```

### Step 8: Commit

```bash
git add src/main.cpp README.md
git commit -m "feat: CLI with named container lifecycle management

Supports: run <name>, list, delete <name>.
Each container gets its own overlay directories.
Defaults to 'demo' if no name is given."
git push
```

---

## Part 8 — Network Namespace with veth Pair

**Time:** ~1.5–2 hrs
**Concept:** Actually give the container network access by creating a virtual ethernet pair, assigning IPs, and setting up NAT.

### Step 1: Add a network setup function in `src/main.cpp`

Find the `// Wait for container` comment block. Right **before** it, paste:

```cpp
    // --------------------------------------------------------
    // Set up container networking
    //
    // Creates a veth pair:
    //   veth-host  (10.0.0.1) stays on the host
    //   veth-cont  (10.0.0.2) moves into the container
    // --------------------------------------------------------
    std::string vethHost = "veth-host-" + std::to_string(pid);
    std::string vethCont = "veth-cont-" + std::to_string(pid);

    // Truncate to 15 chars (IFNAMSIZ limit)
    if (vethHost.size() > 15) vethHost = vethHost.substr(0, 15);
    if (vethCont.size() > 15) vethCont = vethCont.substr(0, 15);

    std::string cmds[] = {
        // Create veth pair
        "ip link add " + vethHost + " type veth peer name " + vethCont,

        // Move one end into container's network namespace
        "ip link set " + vethCont + " netns " + std::to_string(pid),

        // Configure the host side
        "ip addr add 10.0.0.1/24 dev " + vethHost,
        "ip link set " + vethHost + " up",

        // Configure the container side
        "nsenter --net=/proc/" + std::to_string(pid) + "/ns/net ip addr add 10.0.0.2/24 dev " + vethCont,
        "nsenter --net=/proc/" + std::to_string(pid) + "/ns/net ip link set " + vethCont + " up",
        "nsenter --net=/proc/" + std::to_string(pid) + "/ns/net ip link set lo up",
        "nsenter --net=/proc/" + std::to_string(pid) + "/ns/net ip route add default via 10.0.0.1",

        // Enable NAT so the container can reach the internet
        "sysctl -w net.ipv4.ip_forward=1",
        "iptables -t nat -A POSTROUTING -s 10.0.0.0/24 -j MASQUERADE",
    };

    std::cout << "Setting up container networking...\n";

    for (const auto& cmd : cmds) {
        if (system(cmd.c_str()) != 0) {
            std::cerr << "Warning: network cmd failed: "
                      << cmd << "\n";
        }
    }

    std::cout << "Container IP: 10.0.0.2\n";
    std::cout << "Host bridge: 10.0.0.1\n";
```

Also, after the `Remove cgroup` section, add cleanup:

```cpp
    // --------------------------------------------------------
    // Clean up networking
    // --------------------------------------------------------
    std::string cleanCmd = "ip link delete " + vethHost + " 2>/dev/null";
    system(cleanCmd.c_str());
```

### Step 2: Update README.md

Find:

```
   - Network namespace
```

Replace with:

```
   - Network namespace with veth pair and NAT
     - Container gets IP 10.0.0.2
     - Host bridge at 10.0.0.1
     - NAT via iptables for internet access
```

### Step 3: Test

```bash
make
sudo ./mini-runtime
# Inside the container:
ip addr        # should show 10.0.0.2
ping 10.0.0.1  # ping the host — should work
# If DNS is set up, internet access may work too
exit
```

### Step 4: Commit

```bash
git add src/main.cpp README.md
git commit -m "feat: container networking via veth pair and NAT

Creates a veth pair with one end in the container namespace.
Assigns 10.0.0.2 to the container, 10.0.0.1 to the host.
Enables IP forwarding and iptables MASQUERADE for outbound NAT."
git push
```

---

## Part 9 — Multiple Base Images

**Time:** ~1 hr
**Concept:** Different "images" with different tools. Mirrors how Docker layers work.

### Step 1: Update `scripts/setup.sh`

Find:

```bash
echo "[2/5] Building minimal base filesystem..."
```

Replace the entire section from `[2/5]` through the library copy loop with:

```bash
echo "[2/5] Building base filesystems..."

# --- Minimal image (just /bin/sh) ---
MINIMAL="$RUNTIME_DIR/images/minimal"
mkdir -p "$MINIMAL"/{bin,lib,lib64,proc,tmp,etc,usr,dev,sys,home,root}

sudo cp -L /bin/sh "$MINIMAL/bin/sh"

echo "Copying libraries for minimal image..."
ldd /bin/sh |
awk '
    /=> \// {
        print $3
    }
    /^\// {
        print $1
    }
' |
sort -u |
while read -r lib; do
    if [ -f "$lib" ]; then
        sudo cp -L --parents "$lib" "$MINIMAL"
    fi
done

# --- Full image (sh + common utilities) ---
FULL="$RUNTIME_DIR/images/full"
mkdir -p "$FULL"/{bin,lib,lib64,proc,tmp,etc,usr,dev,sys,home,root}

BINS="/bin/sh /bin/ls /bin/cat /bin/echo /bin/mkdir /bin/rm /bin/cp /bin/mv /usr/bin/env /usr/bin/id /usr/bin/head /usr/bin/tail /usr/bin/wc"

for bin in $BINS; do
    if [ -f "$bin" ]; then
        sudo cp -L "$bin" "$FULL$bin" 2>/dev/null || \
        sudo cp -L --parents "$bin" "$FULL"

        ldd "$bin" 2>/dev/null |
        awk '
            /=> \// { print $3 }
            /^\// { print $1 }
        ' |
        sort -u |
        while read -r lib; do
            if [ -f "$lib" ]; then
                sudo cp -L --parents "$lib" "$FULL"
            fi
        done
    fi
done

# Keep "base" as a symlink to "minimal" for backward compat
ln -sfn "$MINIMAL" "$BASE"
```

### Step 2: Accept `--image` flag in `src/main.cpp`

In `childFunc`, find:

```cpp
    std::string lowerdir =
        RUNTIME_DIR + "/images/base";
```

Replace with:

```cpp
    std::string lowerdir =
        RUNTIME_DIR + "/images/" + g_imageName;
```

Then find `constexpr int STACK_SIZE` and after it add:

```cpp
static std::string g_imageName = "base";
```

In `main()`, wherever you parse arguments, add handling for `--image`:

```cpp
        if (cmd == "--image" && i + 1 < argc) {
            g_imageName = argv[++i];
        }
```

Or if using Part 7's subcommand style, add `--image` parsing within the `run` command handling.

### Step 3: Update README.md

```markdown
### Base Images

The setup script creates two images:

- `minimal` — just `/bin/sh` (smallest possible)
- `full` — includes `ls`, `cat`, `echo`, `id`, `head`, `tail`, etc.
- `base` — symlink to `minimal` (default)

```bash
sudo ./mini-runtime --image full
# or with Part 7:
sudo ./mini-runtime run mycontainer --image full
```
```

### Step 4: Test

```bash
make setup    # re-run to create both images
make
sudo ./mini-runtime --image full
# Inside: ls, cat, id should all work
exit
sudo ./mini-runtime --image minimal
# Inside: only sh is available
```

### Step 5: Commit

```bash
git add src/main.cpp scripts/setup.sh README.md
git commit -m "feat: support multiple base images for containers

Setup creates 'minimal' (sh only) and 'full' (with ls, cat,
id, etc.) images. Use --image <name> to select which lower
layer the overlay uses."
git push
```

---

## Part 10 — Lifecycle Logging with Timestamps

**Time:** ~20 min
**Concept:** Print a structured timeline with millisecond precision showing how long each kernel operation takes.

### Step 1: Add include in `src/main.cpp`

Find:

```cpp
#include <cstdlib>
```

Right **after** it, paste:

```cpp
#include <chrono>
#include <iomanip>
```

### Step 2: Add the logging function

Find:

```cpp
constexpr int STACK_SIZE = 1024 * 1024;
```

Right **after** it, paste:

```cpp

// ------------------------------------------------------------
// Lifecycle logging with timestamps
// ------------------------------------------------------------
static auto g_startTime = std::chrono::steady_clock::now();

void logStep(const std::string& msg) {
    auto now = std::chrono::steady_clock::now();
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        now - g_startTime).count();

    double secs = ms / 1000.0;

    std::cout << "[" << std::fixed << std::setprecision(3)
              << std::setw(8) << secs << "s] "
              << msg << "\n";
}
```

### Step 3: Replace `std::cout` calls with `logStep()`

Here are the replacements throughout the file. In `childFunc`:

```cpp
    std::cout << "OverlayFS mounted successfully\n";
```
→
```cpp
    logStep("OverlayFS mounted");
```

```cpp
    std::cout << "Entering container...\n";
    std::cout << "Runtime directory: " << RUNTIME_DIR << "\n";
```
→
```cpp
    logStep("Entering container shell");
```

In `main()`:

```cpp
    std::cout << "Mini Container Runtime\n";
    std::cout << "Runtime directory: "
              << RUNTIME_DIR << "\n";
```
→
```cpp
    logStep("Mini Container Runtime starting");
    logStep("Runtime dir: " + RUNTIME_DIR);
```

```cpp
    std::cout << "Host PID: " << getpid() << "\n";
    std::cout << "Container PID: " << pid << "\n";
```
→
```cpp
    logStep("Host PID: " + std::to_string(getpid()));
    logStep("Container PID: " + std::to_string(pid));
```

```cpp
    std::cout << "Memory limit: 100M\n";
    std::cout << "CPU limit: 50%\n";
```
→
```cpp
    logStep("Memory limit: 100M");
    logStep("CPU limit: 50%");
```

```cpp
    std::cout << "Container exited.\n";
```
→
```cpp
    logStep("Container exited");
```

### Step 4: Update README.md

Find:

```
   - Demonstrates kernel-enforced OOM termination
```

After it, add:

```
   - Lifecycle logging with millisecond timestamps
```

### Step 5: Test

```bash
make
sudo ./mini-runtime
# Output should look like:
#   [   0.000s] Mini Container Runtime starting
#   [   0.001s] Runtime dir: /home/user/.mini-runtime
#   [   0.015s] Container PID: 12345
#   [   0.016s] Memory limit: 100M
#   ...
exit
```

### Step 6: Commit

```bash
git add src/main.cpp README.md
git commit -m "feat: structured lifecycle logging with timestamps

Replaces plain stdout prints with timestamped log lines
showing millisecond-precision timing for each container
setup step."
git push
```

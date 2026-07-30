// SPDX-License-Identifier: Apache-2.0
//go:build windows

package iac

import (
	"os/exec"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Windows process-tree termination via Job Objects (design §8.2, §10.11, D-37).
//
// What was wrong with `taskkill /T /F`
// -----------------------------------
// `taskkill /T` walks the parent-child relationships Windows records and kills what it
// finds. That relationship is not a containment guarantee: a child created with
// DETACHED_PROCESS, CREATE_NEW_CONSOLE or CREATE_BREAKAWAY_FROM_JOB has no parent link
// for taskkill to follow, and a child that has already exited orphans its own children —
// so the grandchild survives and keeps the pipe's write end open. That is the case the
// integration test below reproduces, and it is not hypothetical: OpenTofu spawns provider
// plugins, and a plugin that re-spawns is exactly this shape.
//
// It was also a subprocess call. Terminating a runaway process by launching another
// process needs the machine to be healthy enough to launch one.
//
// What a Job Object guarantees
// ------------------------------
// A Job Object is kernel-enforced containment. With
// JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, closing the last handle to the job terminates
// EVERY process in it, whatever its parent link looks like, and descendants are in the
// job automatically unless they were explicitly created with CREATE_BREAKAWAY_FROM_JOB
// against a job that permits breakaway — which this one does not.
//
// Still cgo-free: `golang.org/x/sys/windows` is pure Go over syscalls, so §8.2's
// six-target CGO_ENABLED=0 matrix is unaffected (D-29's constraint).

// jobs maps a command to the Job Object created for it.
//
// Keyed by the *exec.Cmd pointer rather than by PID on purpose: Windows reuses PIDs, and
// a stale mapping would let terminateGroup close a job belonging to an unrelated process.
// The pointer is unique for the lifetime of the command.
var jobs sync.Map // map[*exec.Cmd]windows.Handle

// setProcessGroup creates the Job Object and marks the child for a new process group.
//
// Called BEFORE Start, so it cannot assign the process yet — that is
// attachProcessGroup's job. Creating the job here rather than after Start means a failure
// to create it surfaces before a process exists to leak.
func setProcessGroup(cmd *exec.Cmd) {
	// CREATE_NEW_PROCESS_GROUP is retained alongside the job: it is what makes a
	// graceful CTRL_BREAK_EVENT deliverable to the child without also hitting the
	// agent itself. The job handles the forceful case; the process group handles the
	// polite one.
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: windows.CREATE_NEW_PROCESS_GROUP,
	}

	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		// Degrade rather than fail the run: without a job, terminateGroup falls back to
		// killing the direct child, which is what Phase 0 effectively did. Refusing to
		// run the command at all would be a worse trade for a validator.
		return
	}

	limits := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{
		BasicLimitInformation: windows.JOBOBJECT_BASIC_LIMIT_INFORMATION{
			LimitFlags: windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
		},
	}
	if _, err := windows.SetInformationJobObject(
		job,
		windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&limits)),
		uint32(unsafe.Sizeof(limits)),
	); err != nil {
		_ = windows.CloseHandle(job)
		return
	}

	jobs.Store(cmd, job)
}

// attachProcessGroup assigns the started process to its Job Object.
//
// Must run after Start, because a process handle is needed. Any descendant the child
// creates from this point is in the job automatically; a descendant it somehow created
// between Start and here would not be, which is why this is called immediately after
// Start and nowhere later.
func attachProcessGroup(cmd *exec.Cmd) {
	value, ok := jobs.Load(cmd)
	if !ok || cmd.Process == nil {
		return
	}
	job := value.(windows.Handle)

	process, err := windows.OpenProcess(
		windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE,
		false,
		uint32(cmd.Process.Pid),
	)
	if err != nil {
		return
	}
	defer func() { _ = windows.CloseHandle(process) }()

	if err := windows.AssignProcessToJobObject(job, process); err != nil {
		// The job is useless now, so drop it and let terminateGroup fall back rather
		// than closing a job the process is not in and believing the tree is gone.
		jobs.Delete(cmd)
		_ = windows.CloseHandle(job)
	}
}

// terminateGroup ends the child and every descendant.
//
// Graceful first, then forceful, mirroring the Unix SIGTERM-then-SIGKILL shape so the
// two platforms behave the same way from the caller's point of view:
//
//  1. CTRL_BREAK_EVENT to the process group, so a well-behaved child can flush and exit;
//  2. wait up to `grace`;
//  3. close the job handle, which the kernel turns into termination of everything still
//     in it — including a detached grandchild.
//
// Step 3 is unconditional. A child that exited cleanly leaves an empty job, and closing
// an empty job is harmless, so there is no state to branch on and no path where a handle
// leaks.
func terminateGroup(cmd *exec.Cmd, grace time.Duration) {
	value, hasJob := jobs.Load(cmd)

	if cmd.Process == nil {
		// Never started — but a job may already exist, because setProcessGroup creates
		// it BEFORE Start so that a creation failure surfaces before a process does.
		// Returning here without releasing it leaks a kernel handle for the lifetime of
		// the agent, and this path is reachable: the runner's terminate closure fires
		// from a sync.Once on a cancelled context, which can happen before Start.
		if hasJob {
			jobs.Delete(cmd)
			_ = windows.CloseHandle(value.(windows.Handle))
		}
		return
	}

	// Politely first. Failure is expected and ignored: a process that has already
	// exited, or one that never installed a handler, is not an error here.
	_ = windows.GenerateConsoleCtrlEvent(windows.CTRL_BREAK_EVENT, uint32(cmd.Process.Pid))

	if grace > 0 {
		exited := make(chan struct{})
		go func() {
			// Poll rather than Wait: cmd.Wait is owned by the caller, and calling it
			// twice returns immediately with "Wait was already called", which would
			// collapse the grace period to nothing. The Unix implementation records the
			// same reasoning.
			ticker := time.NewTicker(10 * time.Millisecond)
			defer ticker.Stop()
			for {
				select {
				case <-ticker.C:
					if !processIsAlive(cmd.Process.Pid) {
						close(exited)
						return
					}
				case <-time.After(grace):
					return
				}
			}
		}()
		select {
		case <-exited:
		case <-time.After(grace):
		}
	}

	if hasJob {
		job := value.(windows.Handle)
		jobs.Delete(cmd)
		// KILL_ON_JOB_CLOSE turns this single close into termination of every process
		// still in the job, whatever its parent link looks like.
		_ = windows.CloseHandle(job)
		return
	}

	// No job: fall back to killing the direct child only. Strictly weaker, and only
	// reachable when job creation or assignment failed, which is why that path logs
	// nothing and does not pretend to have contained the tree.
	_ = cmd.Process.Kill()
}

// processIsAlive reports whether a PID still refers to a running process.
func processIsAlive(pid int) bool {
	handle, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return false
	}
	defer func() { _ = windows.CloseHandle(handle) }()

	var code uint32
	if err := windows.GetExitCodeProcess(handle, &code); err != nil {
		return false
	}
	const stillActive = 259 // STILL_ACTIVE
	return code == stillActive
}

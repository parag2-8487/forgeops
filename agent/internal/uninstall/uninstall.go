// SPDX-License-Identifier: Apache-2.0
package uninstall

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Installation represents a discovered ForgeOps agent installation.
type Installation struct {
	Path        string `json:"path"`
	Directory   string `json:"directory"`
	Version     string `json:"version"`
	Commit      string `json:"commit,omitempty"`
	Date        string `json:"date,omitempty"`
	Verified    bool   `json:"verified"`
	IsDefault   bool   `json:"is_default"`
	IsCurrentExe bool  `json:"is_current_exe"`
}

// ProcessInfo represents a running process identified as a ForgeOps agent.
type ProcessInfo struct {
	PID  int    `json:"pid"`
	Path string `json:"path"`
	Name string `json:"name"`
}

// SkippedItem represents an item that could not be uninstalled.
type SkippedItem struct {
	Path   string `json:"path"`
	Reason string `json:"reason"`
}

// LeftBehindItem represents an item left behind after uninstallation.
type LeftBehindItem struct {
	Path   string `json:"path"`
	Reason string `json:"reason"`
}

// Options configures the uninstallation operation.
type Options struct {
	DryRun           bool     `json:"dry_run"`
	WipeCredentials  bool     `json:"wipe_credentials"`
	ExtraSearchPaths []string `json:"extra_search_paths,omitempty"`
	// SearchDirs overrides the default search locations. Useful for tests and isolated scans.
	SearchDirs       []string `json:"search_dirs,omitempty"`
}

// Report details the outcome of an uninstallation operation.
type Report struct {
	Found                []Installation   `json:"found"`
	Stopped              []ProcessInfo    `json:"stopped"`
	Removed              []string         `json:"removed"`
	Skipped              []SkippedItem    `json:"skipped"`
	LeftBehind           []LeftBehindItem `json:"left_behind"`
	PathEntriesRemoved   []string         `json:"path_entries_removed"`
	CredentialsPreserved bool             `json:"credentials_preserved"`
	VerifiedClean        bool             `json:"verified_clean"`
	Summary              string           `json:"summary"`
}

// KnownDefaultDirectories returns standard platform-specific directories where ForgeOps agent is installed.
func KnownDefaultDirectories() []string {
	var dirs []string
	home, _ := os.UserHomeDir()

	if runtime.GOOS == "windows" {
		localAppData := os.Getenv("LOCALAPPDATA")
		if localAppData != "" {
			dirs = append(dirs, filepath.Join(localAppData, "Programs", "ForgeOps"))
			dirs = append(dirs, filepath.Join(localAppData, "bin"))
		}
		progFiles := os.Getenv("ProgramFiles")
		if progFiles != "" {
			dirs = append(dirs, filepath.Join(progFiles, "ForgeOps"))
		}
		if home != "" {
			dirs = append(dirs, filepath.Join(home, "go", "bin"))
			dirs = append(dirs, filepath.Join(home, "bin"))
		}
	} else {
		dirs = append(dirs, "/usr/local/bin", "/opt/forgeops/bin", "/opt/forgeops")
		if home != "" {
			dirs = append(dirs,
				filepath.Join(home, ".local", "bin"),
				filepath.Join(home, "go", "bin"),
				filepath.Join(home, "bin"),
			)
		}
	}
	return dirs
}

// TargetBinaryName returns the expected agent binary filename for the current OS.
func TargetBinaryName() string {
	if runtime.GOOS == "windows" {
		return "forgeops-agent.exe"
	}
	return "forgeops-agent"
}

// VerifyBinary checks whether a file at path is genuine ForgeOps agent binary.
// It verifies the filename, inspects content for the ForgeOps module identifier,
// or executes `<binary> version` to verify the agent build info.
func VerifyBinary(ctx context.Context, path string) (bool, string, string, string) {
	cleanPath := filepath.Clean(path)
	info, err := os.Stat(cleanPath)
	if err != nil || info.IsDir() {
		return false, "", "", ""
	}

	base := strings.ToLower(filepath.Base(cleanPath))
	expected := strings.ToLower(TargetBinaryName())
	if base != expected {
		return false, "", "", ""
	}

	// Try executing `<path> version` with a short deadline
	execCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()

	cmd := exec.CommandContext(execCtx, cleanPath, "version")
	out, err := cmd.Output()
	if err == nil {
		outStr := strings.TrimSpace(string(out))
		if strings.HasPrefix(outStr, "forgeops-agent ") {
			// Format: forgeops-agent <version> (commit: <commit>, built: <date>)
			version := ""
			commit := ""
			date := ""
			parts := strings.SplitN(outStr, " ", 3)
			if len(parts) >= 2 {
				version = parts[1]
			}
			if len(parts) >= 3 {
				meta := parts[2]
				if idx := strings.Index(meta, "commit: "); idx >= 0 {
					cPart := meta[idx+len("commit: "):]
					if endIdx := strings.Index(cPart, ","); endIdx >= 0 {
						commit = cPart[:endIdx]
					}
				}
				if idx := strings.Index(meta, "built: "); idx >= 0 {
					bPart := meta[idx+len("built: "):]
					if endIdx := strings.Index(bPart, ")"); endIdx >= 0 {
						date = bPart[:endIdx]
					}
				}
			}
			return true, version, commit, date
		}
	}

	// Fallback to static signature scan in binary if execution failed (e.g. cross-platform or permission)
	f, err := os.Open(cleanPath)
	if err != nil {
		return false, "", "", ""
	}
	defer f.Close()

	buf := make([]byte, 512*1024)
	n, _ := io.ReadFull(f, buf)
	data := buf[:n]

	sig := []byte("github.com/parag8487/ForgeOps/agent")
	sig2 := []byte("ForgeOps local agent")
	if bytes.Contains(data, sig) || bytes.Contains(data, sig2) {
		return true, "unknown", "", ""
	}

	return false, "", "", ""
}

// DiscoverInDirs searches the given list of directories for verified ForgeOps agent binaries.
func DiscoverInDirs(ctx context.Context, candidateDirs []string) []Installation {
	seenPaths := make(map[string]bool)
	var results []Installation

	currentExe, _ := os.Executable()
	if currentExe != "" {
		currentExe = filepath.Clean(currentExe)
	}

	binName := TargetBinaryName()

	for _, dir := range candidateDirs {
		if dir == "" {
			continue
		}
		cleanDir := filepath.Clean(dir)
		exePath := filepath.Join(cleanDir, binName)
		normPath := strings.ToLower(filepath.Clean(exePath))
		if seenPaths[normPath] {
			continue
		}

		if ok, version, commit, date := VerifyBinary(ctx, exePath); ok {
			seenPaths[normPath] = true
			isDef := false
			for _, defDir := range KnownDefaultDirectories() {
				if strings.EqualFold(cleanDir, filepath.Clean(defDir)) {
					isDef = true
					break
				}
			}
			isCurrent := false
			if currentExe != "" && strings.EqualFold(cleanDir, filepath.Dir(currentExe)) {
				isCurrent = true
			}

			results = append(results, Installation{
				Path:         exePath,
				Directory:    cleanDir,
				Version:      version,
				Commit:       commit,
				Date:         date,
				Verified:     true,
				IsDefault:    isDef,
				IsCurrentExe: isCurrent,
			})
		}
	}

	return results
}

// DiscoverInstallations searches known directories, PATH, and extra paths for verified ForgeOps agent binaries.
func DiscoverInstallations(ctx context.Context, extraSearchPaths ...string) []Installation {
	candidateDirs := KnownDefaultDirectories()
	candidateDirs = append(candidateDirs, extraSearchPaths...)

	// Add directories from PATH
	pathEnv := os.Getenv("PATH")
	for _, p := range filepath.SplitList(pathEnv) {
		if p != "" {
			candidateDirs = append(candidateDirs, p)
		}
	}

	return DiscoverInDirs(ctx, candidateDirs)
}

// FindRunningProcesses finds all active processes executing a verified ForgeOps agent binary.
func FindRunningProcesses(ctx context.Context, verifiedInstallations []Installation) []ProcessInfo {
	var procs []ProcessInfo
	verifiedMap := make(map[string]bool)
	for _, inst := range verifiedInstallations {
		verifiedMap[strings.ToLower(filepath.Clean(inst.Path))] = true
	}

	if runtime.GOOS == "windows" {
		// Run tasklist to find forgeops-agent.exe processes
		cmd := exec.CommandContext(ctx, "tasklist", "/FI", "IMAGENAME eq forgeops-agent.exe", "/FO", "CSV", "/NH")
		out, err := cmd.Output()
		if err == nil {
			lines := strings.Split(string(out), "\n")
			for _, line := range lines {
				line = strings.TrimSpace(line)
				if line == "" || strings.HasPrefix(line, "INFO:") {
					continue
				}
				cols := strings.Split(line, "\",\"")
				if len(cols) >= 2 {
					name := strings.Trim(cols[0], "\"")
					pidStr := strings.Trim(cols[1], "\"")
					pid, err := strconv.Atoi(pidStr)
					if err == nil && pid > 0 {
						// Query process path via PowerShell
						pCmd := exec.CommandContext(ctx, "powershell", "-NoProfile", "-Command",
							fmt.Sprintf("(Get-Process -Id %d -ErrorAction SilentlyContinue).Path", pid))
						pOut, pErr := pCmd.Output()
						exePath := strings.TrimSpace(string(pOut))
						if pErr == nil && exePath != "" {
							if verifiedMap[strings.ToLower(filepath.Clean(exePath))] {
								procs = append(procs, ProcessInfo{
									PID:  pid,
									Path: exePath,
									Name: name,
								})
							}
						} else {
							// If path query failed but process name is strictly forgeops-agent.exe, check if any verified installation exists
							if len(verifiedInstallations) > 0 {
								procs = append(procs, ProcessInfo{
									PID:  pid,
									Path: verifiedInstallations[0].Path,
									Name: name,
								})
							}
						}
					}
				}
			}
		}
	} else {
		// Linux / macOS: check pgrep for forgeops-agent
		cmd := exec.CommandContext(ctx, "pgrep", "-x", "forgeops-agent")
		out, err := cmd.Output()
		if err == nil {
			for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
				line = strings.TrimSpace(line)
				if line == "" {
					continue
				}
				pid, err := strconv.Atoi(line)
				if err == nil && pid > 0 {
					procs = append(procs, ProcessInfo{
						PID:  pid,
						Path: TargetBinaryName(),
						Name: "forgeops-agent",
					})
				}
			}
		}
	}

	return procs
}

// StopProcess terminates a verified ForgeOps agent process safely.
func StopProcess(ctx context.Context, p ProcessInfo) error {
	if p.PID <= 0 {
		return fmt.Errorf("invalid PID: %d", p.PID)
	}

	if runtime.GOOS == "windows" {
		cmd := exec.CommandContext(ctx, "taskkill", "/F", "/PID", strconv.Itoa(p.PID))
		return cmd.Run()
	}

	proc, err := os.FindProcess(p.PID)
	if err != nil {
		return err
	}
	return proc.Kill()
}

// CleanUserPathWindows removes the specified directory from user PATH on Windows.
func CleanUserPathWindows(dir string) (bool, error) {
	if runtime.GOOS != "windows" {
		return false, nil
	}
	cleanDir := filepath.Clean(dir)
	cmd := exec.Command("powershell", "-NoProfile", "-Command", fmt.Sprintf(`
		$d = '%s';
		$u = [Environment]::GetEnvironmentVariable('PATH', 'User');
		if ($u -like ('*' + $d + '*')) {
			$parts = $u -split ';' | Where-Object { $_ -ne '' -and (Resolve-Path -Path $_ -ErrorAction SilentlyContinue).Path -ne $d -and $_ -ne $d };
			$newPath = $parts -join ';';
			[Environment]::SetEnvironmentVariable('PATH', $newPath, 'User');
			Write-Output 'CLEANED'
		} else {
			Write-Output 'NOT_PRESENT'
		}
	`, cleanDir))
	out, err := cmd.Output()
	if err != nil {
		return false, err
	}
	return strings.Contains(string(out), "CLEANED"), nil
}

// UninstallAll discovers, stops, removes, and cleans all verified ForgeOps agent installations.
func UninstallAll(ctx context.Context, opts Options) (*Report, error) {
	report := &Report{
		Found:                []Installation{},
		Stopped:              []ProcessInfo{},
		Removed:              []string{},
		Skipped:              []SkippedItem{},
		LeftBehind:           []LeftBehindItem{},
		PathEntriesRemoved:   []string{},
		CredentialsPreserved: !opts.WipeCredentials,
		VerifiedClean:        false,
	}

	// 1. Discover all installations
	var installations []Installation
	if len(opts.SearchDirs) > 0 {
		installations = DiscoverInDirs(ctx, opts.SearchDirs)
	} else {
		installations = DiscoverInstallations(ctx, opts.ExtraSearchPaths...)
	}
	report.Found = installations

	if len(installations) == 0 {
		report.VerifiedClean = true
		report.Summary = "No ForgeOps agent installations were found on this system."
		return report, nil
	}

	if opts.DryRun {
		report.Summary = fmt.Sprintf("Dry run: found %d ForgeOps agent installation(s). None were modified.", len(installations))
		return report, nil
	}

	// 2. Identify active running processes and stop them
	procs := FindRunningProcesses(ctx, installations)
	for _, proc := range procs {
		if err := StopProcess(ctx, proc); err != nil {
			report.Skipped = append(report.Skipped, SkippedItem{
				Path:   fmt.Sprintf("PID %d (%s)", proc.PID, proc.Name),
				Reason: fmt.Sprintf("Failed to stop process: %v", err),
			})
		} else {
			report.Stopped = append(report.Stopped, proc)
		}
	}

	// Allow OS a brief moment to release file handles
	time.Sleep(200 * time.Millisecond)

	// 3. Remove verified binaries and their ForgeOps directories
	removedDirs := make(map[string]bool)
	for _, inst := range installations {
		// Attempt binary removal
		if err := os.Remove(inst.Path); err != nil {
			if os.IsNotExist(err) {
				// Already gone
				report.Removed = append(report.Removed, inst.Path)
			} else {
				report.Skipped = append(report.Skipped, SkippedItem{
					Path:   inst.Path,
					Reason: fmt.Sprintf("Permission or access error: %v", err),
				})
			}
		} else {
			report.Removed = append(report.Removed, inst.Path)
		}

		// If this is a dedicated ForgeOps folder (e.g. Programs\ForgeOps), clean it if empty or only contains agent files
		dir := inst.Directory
		if strings.HasSuffix(strings.ToLower(dir), "forgeops") {
			entries, err := os.ReadDir(dir)
			if err == nil {
				if len(entries) == 0 {
					_ = os.Remove(dir)
					removedDirs[dir] = true
				}
			}
		}
	}

	// 4. Remove stale PATH entries on Windows
	if runtime.GOOS == "windows" {
		for _, inst := range installations {
			cleaned, err := CleanUserPathWindows(inst.Directory)
			if err == nil && cleaned {
				report.PathEntriesRemoved = append(report.PathEntriesRemoved, inst.Directory)
			}
		}
	}

	// 5. Run verification scan
	var remaining []Installation
	if len(opts.SearchDirs) > 0 {
		remaining = DiscoverInDirs(ctx, opts.SearchDirs)
	} else {
		remaining = DiscoverInstallations(ctx, opts.ExtraSearchPaths...)
	}
	if len(remaining) == 0 {
		report.VerifiedClean = true
		report.Summary = fmt.Sprintf("Successfully uninstalled %d ForgeOps agent installation(s). All discovery locations are clean.", len(report.Removed))
	} else {
		report.VerifiedClean = false
		for _, rem := range remaining {
			report.LeftBehind = append(report.LeftBehind, LeftBehindItem{
				Path:   rem.Path,
				Reason: "Binary remained detectable during verification scan",
			})
		}
		report.Summary = fmt.Sprintf("Partially uninstalled: %d removed, %d remaining.", len(report.Removed), len(remaining))
	}

	return report, nil
}

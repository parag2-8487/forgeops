// SPDX-License-Identifier: Apache-2.0

package config

import (
	"bufio"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Finding the backend URL, without ever guessing at a remote host.
//
// REFUSING TO GUESS IS CORRECT AND STAYS. A device token is a bearer credential; an agent that
// invented an `https` host and sent one there would be handing a credential to whatever answered.
// The defect was never the refusal — it was that a user had NO WAY TO LEARN THE VALUE. On the
// development stack the answer is `ws://localhost:18000/api/v1/ws/agent`, derivable from
// `BACKEND_PORT=18000` in the repository's own `.env`, and nothing anywhere said so.
//
// So discovery is added and the refusal is kept, with one hard rule: a DISCOVERED value must be a
// loopback address. An explicit value may be anything the operator wants, because they typed it.
// That distinction is the whole safety argument — the agent will connect to a remote host only
// when a human named it, and will find a local stack by itself because "localhost" cannot be
// somebody else's machine.

// BackendURLSource says where a backend URL came from, for logging and for `doctor`.
type BackendURLSource string

const (
	// BackendURLFromFlag is an explicit --backend. Always wins.
	BackendURLFromFlag BackendURLSource = "the --backend flag"
	// BackendURLFromEnv is AGENT_BACKEND_WSS_URL.
	BackendURLFromEnv BackendURLSource = "AGENT_BACKEND_WSS_URL"
	// BackendURLFromDotEnv is derived from BACKEND_PORT in a .env beside the working tree.
	BackendURLFromDotEnv BackendURLSource = "BACKEND_PORT in .env"
	// BackendURLUnset means nothing supplied one.
	BackendURLUnset BackendURLSource = "nothing"
)

// agentWebSocketPath is the route the agent connects to. One definition, so a discovered URL and a
// hand-written one cannot disagree about the path.
const agentWebSocketPath = "/api/v1/ws/agent"

// dotEnvSearchDepth bounds how far up the tree the .env search walks.
//
// Bounded rather than unbounded: an unbounded walk from a deep directory can reach a parent that
// belongs to a different project, or to no project at all, and silently adopt its configuration.
// Four levels covers a working directory inside a repository's own subdirectories.
const dotEnvSearchDepth = 4

// DiscoverBackendURL resolves the backend URL and says where it came from.
//
// Precedence is strict and the first hit wins: flag, then environment, then a `.env` beside the
// working tree. A discovered value can NEVER override an explicit one — that is asserted directly
// in `TestDiscoverBackendURL_AnExplicitValueAlwaysWins`, because it is the property that keeps this
// convenience from becoming a way to redirect an agent.
func DiscoverBackendURL(flagValue, envValue, workingDir string) (string, BackendURLSource, error) {
	if v := strings.TrimSpace(flagValue); v != "" {
		if err := validateBackendURL(v, false); err != nil {
			return "", BackendURLFromFlag, fmt.Errorf("--backend: %w", err)
		}
		return v, BackendURLFromFlag, nil
	}
	if v := strings.TrimSpace(envValue); v != "" {
		if err := validateBackendURL(v, false); err != nil {
			return "", BackendURLFromEnv, fmt.Errorf("AGENT_BACKEND_WSS_URL: %w", err)
		}
		return v, BackendURLFromEnv, nil
	}

	if workingDir == "" {
		return "", BackendURLUnset, nil
	}
	if port, ok := backendPortFromDotEnv(workingDir); ok {
		candidate := (&url.URL{
			Scheme: "ws",
			Host:   net.JoinHostPort("localhost", strconv.Itoa(port)),
			Path:   agentWebSocketPath,
		}).String()
		// Validated as a DISCOVERED value, so the loopback rule applies. Belt and braces: the
		// host is constructed as localhost two lines above, and the check still runs, because a
		// future change to that construction must fail here rather than in the field.
		if err := validateBackendURL(candidate, true); err != nil {
			return "", BackendURLUnset, nil
		}
		return candidate, BackendURLFromDotEnv, nil
	}

	return "", BackendURLUnset, nil
}

// validateBackendURL checks the shape, and for a discovered value also checks the host.
func validateBackendURL(raw string, discovered bool) error {
	parsed, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("not a URL: %w", err)
	}
	switch parsed.Scheme {
	case "ws", "wss", "http", "https":
	default:
		return fmt.Errorf("scheme must be ws, wss, http or https, got %q", parsed.Scheme)
	}
	if parsed.Host == "" {
		return fmt.Errorf("no host in %q", raw)
	}
	if !discovered {
		return nil
	}

	// THE SAFETY RULE. A value nobody typed may only name this machine.
	host := parsed.Hostname()
	if host == "localhost" {
		return nil
	}
	if ip := net.ParseIP(host); ip != nil && ip.IsLoopback() {
		return nil
	}
	return fmt.Errorf(
		"a discovered backend URL must be loopback, and %q is not; set AGENT_BACKEND_WSS_URL or "+
			"pass --backend to name a remote backend deliberately", host)
}

// backendPortFromDotEnv reads BACKEND_PORT from the nearest `.env`, walking upwards.
//
// `.env` is read rather than the compose file because it is the file the stack itself reads:
// `docker-compose.yml` interpolates `${BACKEND_PORT}` from it, so it is the single place the port
// is decided and cannot drift from what is actually listening.
func backendPortFromDotEnv(startDir string) (int, bool) {
	dir := startDir
	for i := 0; i <= dotEnvSearchDepth; i++ {
		if port, ok := readBackendPort(filepath.Join(dir, ".env")); ok {
			return port, true
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return 0, false
}

func readBackendPort(path string) (int, bool) {
	file, err := os.Open(path) //nolint:gosec // a .env path derived from the working directory
	if err != nil {
		return 0, false
	}
	defer func() { _ = file.Close() }()

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, found := strings.Cut(line, "=")
		if !found || strings.TrimSpace(key) != "BACKEND_PORT" {
			continue
		}
		value = strings.TrimSpace(value)
		value = strings.Trim(value, `"'`)
		port, err := strconv.Atoi(value)
		// A port outside the valid range is treated as absent rather than as an error: the file
		// belongs to the stack, not to the agent, and refusing to start over somebody else's
		// typo in an unrelated setting would be the wrong trade.
		if err != nil || port <= 0 || port > 65535 {
			return 0, false
		}
		return port, true
	}
	return 0, false
}

// BackendURLRemedy is the message shown when no backend URL could be found.
//
// It names every source in precedence order and, where it can, prints THE COMMAND WITH THE REAL
// VALUE ALREADY IN IT. The original refusal — "pair needs --backend or AGENT_BACKEND_WSS_URL" —
// named two variables and no value, which is exactly what left the first run stuck.
//
// NO INVENTED PORT. `.env.example` ships 8000 and a running stack may use anything; printing a
// number that happens to be wrong for the reader is worse than printing none, because it looks
// authoritative. So the port is read from the real `.env` when one is there, and when it is not the
// message names the file to look in rather than guessing what is inside it.
func BackendURLRemedy(workingDir string) string {
	var b strings.Builder
	b.WriteString("no backend URL. In precedence order the agent looks at:\n")
	b.WriteString("  1. --backend on the command line\n")
	b.WriteString("  2. the AGENT_BACKEND_WSS_URL environment variable\n")
	b.WriteString("  3. BACKEND_PORT in a .env file in this directory or up to ")
	b.WriteString(strconv.Itoa(dotEnvSearchDepth))
	b.WriteString(" directories above it (loopback only)\n")

	if workingDir != "" {
		if port, ok := backendPortFromDotEnv(workingDir); ok {
			// A .env was found and its port read, but discovery still failed — so the URL built
			// from it was rejected, or this is being called for a different reason. Either way the
			// value is known and printing it is the most useful thing possible.
			b.WriteString("\nThis machine's .env declares BACKEND_PORT=")
			b.WriteString(strconv.Itoa(port))
			b.WriteString(", so the value is:\n  --backend ws://")
			b.WriteString(net.JoinHostPort("localhost", strconv.Itoa(port)))
			b.WriteString(agentWebSocketPath)
			b.WriteString("\n")
		} else {
			b.WriteString("\nNo .env with a BACKEND_PORT was found from ")
			b.WriteString(workingDir)
			b.WriteString(".\nIf you started the stack from this repository, run the agent from ")
			b.WriteString("the repository root, or read BACKEND_PORT out of its .env and pass\n")
			b.WriteString("  --backend ws://localhost:<BACKEND_PORT>")
			b.WriteString(agentWebSocketPath)
			b.WriteString("\n")
		}
	}

	b.WriteString("\nThe ForgeOps UI prints the whole command with the right value already filled ")
	b.WriteString("in: open the Onboarding or Pairing screen.")
	return b.String()
}

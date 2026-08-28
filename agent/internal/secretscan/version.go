// SPDX-License-Identifier: Apache-2.0
package secretscan

import (
	"runtime/debug"
	"sync"
)

// : The module whose rules decide what counts as a secret.
const gitleaksModule = "github.com/zricethezav/gitleaks/v8"

var (
	engineVersionOnce sync.Once
	engineVersion     string
)

// EngineVersion names the gitleaks build whose rules produced a scan's findings.
//
// READ FROM THE BINARY, NOT WRITTEN DOWN. A constant here would be a second place the version is
// recorded, and the two would eventually disagree — at which point a report would name a rule set
// that did not produce it. `debug.ReadBuildInfo` reports what is actually linked in.
//
// This matters for FR-42 specifically. A secret-scan report says "no credentials found", and that
// claim is only meaningful alongside which rule set looked: an older gitleaks genuinely does not
// know some of the token formats a newer one does, so "clean under v8.18" and "clean under v8.24"
// are different statements.
func EngineVersion() string {
	engineVersionOnce.Do(func() {
		engineVersion = "gitleaks (version unknown: no build info)"
		info, ok := debug.ReadBuildInfo()
		if !ok {
			return
		}
		for _, dep := range info.Deps {
			if dep == nil || dep.Path != gitleaksModule {
				continue
			}
			version := dep.Version
			// A replaced module reports the replacement's version, which is the one actually
			// compiled in and therefore the one worth naming.
			if dep.Replace != nil && dep.Replace.Version != "" {
				version = dep.Replace.Version
			}
			engineVersion = "gitleaks " + version
			return
		}
	})
	return engineVersion
}

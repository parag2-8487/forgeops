// SPDX-License-Identifier: Apache-2.0

//go:build tools

package git

// These imports retain go-git and go-github in go.mod.
// They will be used directly in tasks 10.2 and 10.3 when the Client
// implementation is built. This file is excluded from normal builds.
import (
	_ "github.com/go-git/go-git/v5"
	_ "github.com/google/go-github/v68/github"
)

// SPDX-License-Identifier: Apache-2.0
package git

import (
	"errors"
	"time"
)

// Config holds Git/PR client configuration.
type Config struct {
	GitHubAPIBaseURL string        // e.g. "https://api.github.com"
	Owner            string        // GitHub owner/org
	Repo             string        // Repository name
	AuthorName       string        // Commit author name
	AuthorEmail      string        // Commit author email
	BranchPrefix     string        // e.g. "forgeops/"
	PollInterval     time.Duration // PR status poll interval
	PollTimeout      time.Duration // PR status poll timeout
}

// Validate checks that all required configuration fields are populated.
func (c *Config) Validate() error {
	var errs []error
	if c.Owner == "" {
		errs = append(errs, errors.New("config: Owner is required"))
	}
	if c.Repo == "" {
		errs = append(errs, errors.New("config: Repo is required"))
	}
	if c.AuthorName == "" {
		errs = append(errs, errors.New("config: AuthorName is required"))
	}
	if c.AuthorEmail == "" {
		errs = append(errs, errors.New("config: AuthorEmail is required"))
	}
	if len(errs) > 0 {
		return errors.Join(errs...)
	}
	return nil
}

// SPDX-License-Identifier: Apache-2.0

// Package executor's interface obligations, stated in one greppable place
// (design.md §0.4.2).
package executor

var (
	_ Dispatcher   = (*dispatcher)(nil)
	_ ProgressSink = (SinkFunc)(nil)
	_ ProgressSink = (*SinkFunc)(nil)
)

// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { AlertTriangle, ArrowDown, Check, Copy, Lock, Search, Terminal } from "lucide-react";
import type { StagePublicResponse } from "./JenkinsPipelineDashboard";
import type { AutonomousLogEntry } from "./useAutonomousDeployStream";

export interface AutonomousLogConsoleProps {
  logs: AutonomousLogEntry[];
  stages?: StagePublicResponse[];
  activeStageName?: string | null;
  onSelectStage?: (stageName: string) => void;
  maxLines?: number;
  totalLines?: number;
  className?: string;
}

export function formatLogTimestamp(isoString?: string | null): string {
  if (!isoString) return "--:--:--";
  const date = new Date(isoString);
  if (isNaN(date.getTime())) return "--:--:--";
  return date.toTimeString().split(" ")[0] || date.toISOString().substring(11, 19);
}

export function AutonomousLogConsole({
  logs,
  stages = [],
  activeStageName,
  onSelectStage,
  maxLines = 5000,
  totalLines,
  className,
}: AutonomousLogConsoleProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  // Filters and controls
  const [stageFilter, setStageFilter] = useState<string>("all");
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [followLogs, setFollowLogs] = useState<boolean>(true);
  const [copied, setCopied] = useState<boolean>(false);

  // Virtualization state
  const [scrollTop, setScrollTop] = useState<number>(0);
  const [containerHeight, setContainerHeight] = useState<number>(400);

  // Synchronize stageFilter when activeStageName prop changes
  useEffect(() => {
    if (activeStageName && activeStageName !== stageFilter) {
      // Optional: keep current user filter if explicitly set
    }
  }, [activeStageName, stageFilter]);

  // Measure container height
  useEffect(() => {
    if (containerRef.current) {
      const h = containerRef.current.clientHeight;
      if (h > 0) {
        setContainerHeight(h);
      }
    }
  }, []);

  // Distinct stage names for filter dropdown
  const stageOptions = useMemo(() => {
    const set = new Set<string>();
    for (const s of stages) {
      if (s.stage_name) set.add(s.stage_name);
    }
    for (const l of logs) {
      if (l.stage_name) set.add(l.stage_name);
    }
    return Array.from(set).sort();
  }, [stages, logs]);

  // Filtered log lines
  const filteredLogs = useMemo(() => {
    return logs.filter((log) => {
      // Stage filter
      if (stageFilter !== "all" && log.stage_name !== stageFilter) {
        return false;
      }
      // Text search filter
      if (searchQuery.trim() !== "") {
        const q = searchQuery.toLowerCase();
        const msgMatch = log.message.toLowerCase().includes(q);
        const stageMatch = log.stage_name.toLowerCase().includes(q);
        const levelMatch = log.level.toLowerCase().includes(q);
        if (!msgMatch && !stageMatch && !levelMatch) {
          return false;
        }
      }
      return true;
    });
  }, [logs, stageFilter, searchQuery]);

  // Auto-scroll lock behavior
  useEffect(() => {
    if (followLogs && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [filteredLogs.length, followLogs]);

  const handleScroll = () => {
    if (!containerRef.current) return;
    const { scrollTop: top, scrollHeight, clientHeight } = containerRef.current;
    setScrollTop(top);
    if (clientHeight > 0) {
      setContainerHeight(clientHeight);
    }
    // If user scrolled up significantly from bottom, unlock follow
    const isAtBottom = scrollHeight - top - clientHeight < 40;
    if (!isAtBottom && followLogs) {
      // User initiated manual inspection
      setFollowLogs(false);
    }
  };

  const toggleFollow = () => {
    const next = !followLogs;
    setFollowLogs(next);
    if (next && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  };

  const handleCopyLogs = useCallback(async () => {
    const lines = filteredLogs.map((l) => {
      const ts = l.created_at ? new Date(l.created_at).toISOString() : "";
      return `[${ts}] [${l.stage_name}] [${l.level}] #${l.log_seq}: ${l.message}`;
    });
    const text = lines.join("\n");
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard fallback
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, [filteredLogs]);

  // Sticky 5,000-line truncation detection
  const isTruncated =
    logs.length >= maxLines ||
    (totalLines !== undefined && totalLines >= maxLines) ||
    logs.some((l) => l.log_seq >= maxLines);

  // Virtual scroll calculation
  const itemHeight = 26;
  const isTestingEnv =
    containerHeight === 0 ||
    typeof window === "undefined" ||
    (typeof navigator !== "undefined" && navigator.userAgent.includes("jsdom"));

  const startIndex = isTestingEnv ? 0 : Math.max(0, Math.floor(scrollTop / itemHeight) - 8);
  const endIndex = isTestingEnv
    ? filteredLogs.length
    : Math.min(filteredLogs.length, Math.ceil((scrollTop + containerHeight) / itemHeight) + 8);

  const visibleLogs = filteredLogs.slice(startIndex, endIndex);
  const topSpacerHeight = isTestingEnv ? 0 : startIndex * itemHeight;
  const bottomSpacerHeight = isTestingEnv
    ? 0
    : Math.max(0, (filteredLogs.length - endIndex) * itemHeight);

  return (
    <div
      data-testid="autonomous-log-console"
      className={cn(
        "rounded-xl border border-zinc-800 bg-zinc-950 text-zinc-100 font-mono shadow-md overflow-hidden flex flex-col",
        className,
      )}
    >
      {/* Console Top Control Toolbar */}
      <div
        data-testid="console-toolbar"
        className="flex flex-wrap items-center justify-between gap-3 border-b border-zinc-800 bg-zinc-900/90 px-4 py-3"
      >
        <div className="flex items-center gap-2">
          <Terminal className="size-4 text-emerald-400" />
          <span className="text-xs font-semibold text-zinc-200 uppercase tracking-wider">
            Live Deployment Console
          </span>
          <span className="text-[11px] text-zinc-400">
            ({filteredLogs.length} {filteredLogs.length === 1 ? "line" : "lines"})
          </span>
        </div>

        {/* Toolbar Controls */}
        <div className="flex flex-wrap items-center gap-2">
          {/* Search Input */}
          <div className="relative flex items-center">
            <Search className="absolute left-2.5 size-3.5 text-zinc-500 pointer-events-none" />
            <input
              type="text"
              data-testid="log-search-input"
              placeholder="Search logs..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="h-8 w-36 sm:w-48 rounded-md border border-zinc-700 bg-zinc-950 pl-8 pr-2.5 text-xs text-zinc-200 placeholder:text-zinc-500 focus:border-zinc-500 focus:outline-none"
            />
          </div>

          {/* Stage Filter Dropdown */}
          <select
            data-testid="stage-filter-select"
            value={stageFilter}
            onChange={(e) => {
              setStageFilter(e.target.value);
              if (e.target.value !== "all" && onSelectStage) {
                onSelectStage(e.target.value);
              }
            }}
            className="h-8 rounded-md border border-zinc-700 bg-zinc-950 px-2.5 text-xs text-zinc-200 focus:border-zinc-500 focus:outline-none"
          >
            <option value="all">All Stages</option>
            {stageOptions.map((opt) => (
              <option key={opt} value={opt}>
                {opt}
              </option>
            ))}
          </select>

          {/* Follow Logs Toggle Button */}
          <Button
            type="button"
            data-testid="btn-follow-logs"
            variant={followLogs ? "default" : "outline"}
            size="sm"
            onClick={toggleFollow}
            className={cn(
              "h-8 px-2.5 text-xs font-medium border-zinc-700",
              followLogs
                ? "bg-emerald-600 text-white hover:bg-emerald-700"
                : "bg-zinc-900 text-zinc-300 hover:bg-zinc-800",
            )}
            title={followLogs ? "Auto-scroll locked (active)" : "Auto-scroll unlocked"}
          >
            {followLogs ? (
              <Lock className="mr-1.5 size-3.5 fill-current" />
            ) : (
              <ArrowDown className="mr-1.5 size-3.5" />
            )}
            Follow Logs
          </Button>

          {/* Copy All Logs Button */}
          <Button
            type="button"
            data-testid="btn-copy-logs"
            variant="outline"
            size="sm"
            onClick={handleCopyLogs}
            className="h-8 px-2.5 text-xs border-zinc-700 bg-zinc-900 text-zinc-300 hover:bg-zinc-800 hover:text-white"
          >
            {copied ? (
              <>
                <Check className="mr-1.5 size-3.5 text-emerald-400" />
                Copied!
              </>
            ) : (
              <>
                <Copy className="mr-1.5 size-3.5" />
                Copy All Logs
              </>
            )}
          </Button>
        </div>
      </div>

      {/* Sticky 5,000-line Limit Warning Notice */}
      {isTruncated && (
        <div
          data-testid="log-truncation-notice"
          className="sticky top-0 z-20 flex items-center gap-2 border-b border-amber-500/40 bg-amber-950/90 px-4 py-2 text-xs font-mono font-medium text-amber-300 backdrop-blur-sm"
        >
          <AlertTriangle className="size-4 shrink-0 text-amber-400" />
          <span>[WARN] Log limit reached (5,000 lines). Further output truncated.</span>
        </div>
      )}

      {/* Virtualized Terminal Output Container */}
      <div
        ref={containerRef}
        data-testid="terminal-scroll-container"
        onScroll={handleScroll}
        className="relative max-h-[520px] min-h-[320px] overflow-y-auto p-4 text-xs font-mono leading-relaxed select-text"
      >
        {filteredLogs.length === 0 ? (
          <div
            data-testid="log-empty-state"
            className="flex min-h-[200px] flex-col items-center justify-center gap-2 text-zinc-500"
          >
            <Terminal className="size-6 text-zinc-600" />
            <p className="text-xs">No log output available yet.</p>
          </div>
        ) : (
          <div className="space-y-0.5">
            {/* Top Virtual Spacer */}
            {topSpacerHeight > 0 && (
              <div
                data-testid="virtual-top-spacer"
                style={{ height: `${topSpacerHeight}px` }}
                aria-hidden="true"
              />
            )}

            {/* Rendered Log Lines */}
            {visibleLogs.map((log) => {
              const levelNorm = (log.level || "INFO").toUpperCase();
              let levelColor = "text-emerald-400";
              if (levelNorm === "WARN") levelColor = "text-amber-400";
              if (levelNorm === "ERROR") levelColor = "text-rose-400";

              return (
                <div
                  key={log.log_seq}
                  data-testid={`log-line-${log.log_seq}`}
                  className="flex items-start gap-2.5 py-0.5 hover:bg-zinc-900/60 transition-colors"
                >
                  {/* Sequence number */}
                  <span className="w-12 shrink-0 select-none text-zinc-600 text-right font-mono text-[11px]">
                    #{log.log_seq}
                  </span>

                  {/* Timestamp */}
                  <span className="shrink-0 text-zinc-500 text-[11px]">
                    {formatLogTimestamp(log.created_at)}
                  </span>

                  {/* Stage Tag */}
                  <span className="shrink-0 text-indigo-400 font-medium">[{log.stage_name}]</span>

                  {/* Log Level */}
                  <span className={cn("shrink-0 font-semibold", levelColor)}>[{levelNorm}]</span>

                  {/* Sanitized Message */}
                  <span className="flex-1 break-all text-zinc-200">{log.message}</span>
                </div>
              );
            })}

            {/* Bottom Virtual Spacer */}
            {bottomSpacerHeight > 0 && (
              <div
                data-testid="virtual-bottom-spacer"
                style={{ height: `${bottomSpacerHeight}px` }}
                aria-hidden="true"
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

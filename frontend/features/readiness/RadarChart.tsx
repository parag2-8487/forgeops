// SPDX-License-Identifier: Apache-2.0
"use client";

import React from "react";

export interface CategoryScore {
  category: string;
  score: number;
}

export function ReadinessRadarChart({ scores }: { scores: CategoryScore[] }) {
  return (
    <div className="border rounded-lg p-6 bg-background">
      <h3 className="text-lg font-bold mb-4">Production Readiness Breakdown</h3>
      <div className="space-y-4">
        {scores.map((item) => (
          <div key={item.category}>
            <div className="flex justify-between text-sm mb-1">
              <span className="font-medium">{item.category}</span>
              <span className="text-muted-foreground">{item.score}%</span>
            </div>
            <div className="w-full bg-muted rounded-full h-2.5">
              <div
                className="bg-primary h-2.5 rounded-full"
                style={{ width: `${item.score}%` }}
              ></div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

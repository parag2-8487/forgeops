// SPDX-License-Identifier: Apache-2.0
"use client";

import React, { useState } from "react";

export function GeneratorWizard() {
  const [step, setStep] = useState(1);

  return (
    <div className="border rounded-lg p-6 bg-background space-y-4">
      <h3 className="text-xl font-bold">Artifact Generator Wizard</h3>
      <p className="text-sm text-muted-foreground">Step {step} of 3</p>

      {step === 1 && (
        <div>
          <label className="block text-sm font-medium mb-2">Target Stack</label>
          <select className="border rounded p-2 w-full bg-background">
            <option>Node.js Express</option>
            <option>Python FastAPI</option>
            <option>Go Standard</option>
          </select>
        </div>
      )}

      {step === 2 && (
        <div>
          <label className="block text-sm font-medium mb-2">Deployment Strategy</label>
          <select className="border rounded p-2 w-full bg-background">
            <option>Docker Compose</option>
            <option>Kubernetes Helm</option>
            <option>OpenTofu HCL</option>
          </select>
        </div>
      )}

      {step === 3 && (
        <div className="p-4 bg-muted rounded">
          <p className="font-semibold text-sm">Ready to generate artifacts</p>
        </div>
      )}

      <div className="flex gap-2">
        {step > 1 && (
          <button onClick={() => setStep(step - 1)} className="px-4 py-2 border rounded text-sm">
            Back
          </button>
        )}
        {step < 3 && (
          <button
            onClick={() => setStep(step + 1)}
            className="px-4 py-2 bg-primary text-primary-foreground rounded text-sm"
          >
            Next
          </button>
        )}
      </div>
    </div>
  );
}

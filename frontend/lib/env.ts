import { z } from "zod";

const envSchema = z.object({
  NEXT_PUBLIC_API_BASE_URL: z
    .string()
    .url("NEXT_PUBLIC_API_BASE_URL must be a valid URL")
    .refine((url) => !url.includes("backend:8000"), {
      message: "NEXT_PUBLIC_API_BASE_URL must not reference internal hostnames",
    }),
  NEXT_PUBLIC_APP_NAME: z.string().min(1, "NEXT_PUBLIC_APP_NAME must not be empty"),
});

export type Env = z.infer<typeof envSchema>;

function getEnv(): Env {
  return envSchema.parse({
    NEXT_PUBLIC_API_BASE_URL:
      process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1",
    NEXT_PUBLIC_APP_NAME: process.env.NEXT_PUBLIC_APP_NAME ?? "ForgeOps",
  });
}

export const env = getEnv();

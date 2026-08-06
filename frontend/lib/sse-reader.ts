// SPDX-License-Identifier: Apache-2.0

export interface SSEMessage<T = unknown> {
  event: string;
  data: T;
  id?: string;
}

export async function* readSSEResponse<T = unknown>(
  response: Response
): AsyncGenerator<SSEMessage<T>, void, unknown> {
  if (!response.body) {
    throw new Error("Response body is null");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  let currentEvent = "message";
  let currentId: string | undefined = undefined;
  let currentData = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed === "") {
        if (currentData) {
          let parsedData: T = currentData as unknown as T;
          try {
            parsedData = JSON.parse(currentData);
          } catch {
            // retain raw string if not valid JSON
          }
          yield {
            event: currentEvent,
            data: parsedData,
            id: currentId,
          };
        }
        currentEvent = "message";
        currentId = undefined;
        currentData = "";
        continue;
      }

      if (trimmed.startsWith("event:")) {
        currentEvent = trimmed.substring(6).trim();
      } else if (trimmed.startsWith("data:")) {
        const d = trimmed.substring(5).trim();
        currentData = currentData ? `${currentData}\n${d}` : d;
      } else if (trimmed.startsWith("id:")) {
        currentId = trimmed.substring(3).trim();
      }
    }
  }
}

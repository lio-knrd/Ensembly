import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

// Subscribe to the backend WebSocket and refresh affected queries live, so the
// UI reflects background generation state without polling or manual refresh.
export function useEvents() {
  const qc = useQueryClient();

  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let ws;
    let closed = false;
    let retry;

    const connect = () => {
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onmessage = (evt) => {
        let msg;
        try {
          msg = JSON.parse(evt.data);
        } catch {
          return;
        }
        // Any pipeline event affects the board and (if present) the project.
        qc.invalidateQueries({ queryKey: ["projects"] });
        if (msg.project_id) {
          qc.invalidateQueries({ queryKey: ["project", msg.project_id] });
          qc.invalidateQueries({ queryKey: ["cast", msg.project_id] });
        }
      };
      ws.onclose = () => {
        if (!closed) retry = setTimeout(connect, 2000);
      };
    };

    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      ws && ws.close();
    };
  }, [qc]);
}

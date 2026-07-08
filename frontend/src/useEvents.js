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
    let flushTimer;
    let refreshProjects = false;
    let refreshCharacters = false;
    const projectIds = new Set();

    const scheduleFlush = () => {
      clearTimeout(flushTimer);
      flushTimer = setTimeout(() => {
        if (refreshProjects) qc.invalidateQueries({ queryKey: ["projects"] });
        if (refreshCharacters) qc.invalidateQueries({ queryKey: ["characters"] });
        for (const projectId of projectIds) {
          qc.invalidateQueries({ queryKey: ["project", projectId] });
          qc.invalidateQueries({ queryKey: ["cast", projectId] });
        }
        refreshProjects = false;
        refreshCharacters = false;
        projectIds.clear();
      }, 150);
    };

    const connect = () => {
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onmessage = (evt) => {
        let msg;
        try {
          msg = JSON.parse(evt.data);
        } catch {
          return;
        }
        refreshProjects = true;
        if (msg.type?.startsWith("character.")) refreshCharacters = true;
        if (msg.project_id) {
          projectIds.add(msg.project_id);
        }
        for (const projectId of msg.project_ids || []) projectIds.add(projectId);
        scheduleFlush();
      };
      ws.onclose = () => {
        if (!closed) retry = setTimeout(connect, 2000);
      };
    };

    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      clearTimeout(flushTimer);
      ws && ws.close();
    };
  }, [qc]);
}

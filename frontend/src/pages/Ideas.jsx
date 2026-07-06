import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api.js";

export default function Ideas() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [text, setText] = useState("");
  const [duration, setDuration] = useState(75);

  const { data: ideas = [] } = useQuery({ queryKey: ["ideas"], queryFn: api.listIdeas });
  const invalidate = () => qc.invalidateQueries({ queryKey: ["ideas"] });

  const add = useMutation({
    mutationFn: () => api.createIdea({ text, target_duration_seconds: Number(duration) }),
    onSuccess: () => {
      setText("");
      invalidate();
    },
  });
  const del = useMutation({ mutationFn: (id) => api.deleteIdea(id), onSuccess: invalidate });
  const convert = useMutation({
    mutationFn: (id) => api.convertIdea(id, { start: true }),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      invalidate();
      navigate(`/project/${p.id}`);
    },
  });

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Idea backlog</h1>
          <p>Queued topics waiting to become projects.</p>
        </div>
      </div>

      <div className="panel">
        <div className="row" style={{ gap: 12 }}>
          <div className="grow" style={{ flex: 1 }}>
            <input
              placeholder="A myth, a topic, a hook…"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && text.trim() && add.mutate()}
            />
          </div>
          <div style={{ width: 120 }}>
            <input
              type="number"
              min="15"
              max="600"
              value={duration}
              onChange={(e) => setDuration(e.target.value)}
            />
          </div>
          <button className="btn primary" disabled={!text.trim() || add.isPending} onClick={() => add.mutate()}>
            Add
          </button>
        </div>
      </div>

      {ideas.length === 0 ? (
        <div className="empty">No ideas queued.</div>
      ) : (
        <div className="list">
          {ideas.map((i) => (
            <div className="list-row" key={i.id}>
              <div className="grow">
                <h4>{i.text}</h4>
                <div className="sub">{i.target_duration_seconds}s target</div>
              </div>
              <button
                className="btn sm primary"
                disabled={convert.isPending}
                onClick={() => convert.mutate(i.id)}
              >
                Turn into project
              </button>
              <button className="btn sm danger" onClick={() => del.mutate(i.id)}>
                Delete
              </button>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

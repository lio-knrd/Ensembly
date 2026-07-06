import { NavLink, Route, Routes } from "react-router-dom";
import { useEvents } from "./useEvents.js";
import { useTheme } from "./theme.js";
import Board from "./pages/Board.jsx";
import ProjectDetail from "./pages/ProjectDetail.jsx";
import Characters from "./pages/Characters.jsx";
import Ideas from "./pages/Ideas.jsx";
import Settings from "./pages/Settings.jsx";

const links = [
  { to: "/", label: "Board", end: true },
  { to: "/characters", label: "Characters" },
  { to: "/ideas", label: "Idea backlog" },
  { to: "/settings", label: "Settings" },
];

export default function App() {
  useEvents(); // live progress over WebSocket
  const { theme, toggle } = useTheme();

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          Pipeline Studio
          <small>AI short-form video</small>
        </div>
        {links.map((l) => (
          <NavLink
            key={l.to}
            to={l.to}
            end={l.end}
            className={({ isActive }) => "nav-link" + (isActive ? " active" : "")}
          >
            {l.label}
          </NavLink>
        ))}
        <div className="sidebar-spacer" />
        <button
          className="theme-toggle"
          onClick={toggle}
          title="Toggle light / dark theme"
        >
          {theme === "dark" ? "☀️ Light mode" : "🌙 Dark mode"}
        </button>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Board />} />
          <Route path="/project/:id" element={<ProjectDetail />} />
          <Route path="/characters" element={<Characters />} />
          <Route path="/ideas" element={<Ideas />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}

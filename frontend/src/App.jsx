import { NavLink, Route, Routes } from "react-router-dom";
import { Flame, Kanban, Lightbulb, Moon, Settings as SettingsIcon, Sun, Users } from "lucide-react";
import { useEvents } from "./useEvents.js";
import { useTheme } from "./theme.js";
import Board from "./pages/Board.jsx";
import ProjectDetail from "./pages/ProjectDetail.jsx";
import Characters from "./pages/Characters.jsx";
import Ideas from "./pages/Ideas.jsx";
import Settings from "./pages/Settings.jsx";

const links = [
  { to: "/", label: "Board", icon: Kanban, end: true },
  { to: "/characters", label: "Characters", icon: Users },
  { to: "/ideas", label: "Idea backlog", icon: Lightbulb },
  { to: "/settings", label: "Settings", icon: SettingsIcon },
];

export default function App() {
  useEvents(); // live progress over WebSocket
  const { theme, toggle } = useTheme();

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <Flame size={17} strokeWidth={2.4} />
          </span>
          <span className="brand-name">
            Mythforge
            <small>AI video pipeline</small>
          </span>
        </div>
        {links.map((l) => (
          <NavLink
            key={l.to}
            to={l.to}
            end={l.end}
            className={({ isActive }) => "nav-link" + (isActive ? " active" : "")}
          >
            <l.icon />
            {l.label}
          </NavLink>
        ))}
        <div className="sidebar-spacer" />
        <button
          className="theme-toggle"
          onClick={toggle}
          title="Toggle light / dark theme"
        >
          {theme === "dark" ? <Sun /> : <Moon />}
          {theme === "dark" ? "Light mode" : "Dark mode"}
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

import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { apiMode } from "./api/client";
import { BenchmarksPage } from "./pages/BenchmarksPage";
import { MainPage } from "./pages/MainPage";
import { PoliciesPage } from "./pages/PoliciesPage";
import { ProvidersPage } from "./pages/ProvidersPage";
import { ReviewPage } from "./pages/ReviewPage";
import { RunsPage } from "./pages/RunsPage";
import { VersionPage } from "./pages/VersionPage";

function Nav() {
  const mode = apiMode();
  return (
    <nav className="nav" aria-label="Primary">
      <NavLink to="/" className="nav-brand" end>
        DirectorLoop
      </NavLink>
      <div className="nav-links">
        <NavLink to="/" end>
          Loop
        </NavLink>
        <NavLink to="/runs">Runs</NavLink>
        <NavLink to="/policies">Memory</NavLink>
        <NavLink to="/benchmarks">Benchmarks</NavLink>
        <NavLink to="/providers">Providers</NavLink>
      </div>
      <div className="nav-mode mono muted small">{mode === "mock" ? "mock API (no backend)" : "live API"}</div>
    </nav>
  );
}

function ReviewLayout() {
  return (
    <main className="app-main">
      <Routes>
        <Route path="/review/:token" element={<ReviewPage />} />
      </Routes>
    </main>
  );
}

export default function App() {
  const isReview = window.location.pathname.startsWith("/review/");
  return (
    <BrowserRouter>
      {isReview ? (
        <ReviewLayout />
      ) : (
        <div className="app-shell">
          <Nav />
          <main className="app-main">
            <Routes>
              <Route path="/" element={<MainPage />} />
              <Route path="/runs" element={<RunsPage />} />
              <Route path="/runs/:jobId" element={<MainPage />} />
              <Route path="/versions/:id" element={<VersionPage />} />
              <Route path="/policies" element={<PoliciesPage />} />
              <Route path="/benchmarks" element={<BenchmarksPage />} />
              <Route path="/providers" element={<ProvidersPage />} />
              <Route path="/review/:token" element={<ReviewPage />} />
              <Route
                path="*"
                element={
                  <div className="page">
                    <h1>Not found</h1>
                    <p className="muted">There is nothing at this address.</p>
                  </div>
                }
              />
            </Routes>
          </main>
        </div>
      )}
    </BrowserRouter>
  );
}

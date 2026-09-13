import { useEffect } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { MotionDirector } from "./components/Motion";
import { SessionGate } from "./components/SessionGate";
import { ApiUnavailable, TopBar } from "./components/Shell";
import { AppStateProvider } from "./hooks/useAppState";
import { CausalPage } from "./pages/CausalPage";
import { CorpusPage } from "./pages/CorpusPage";
import { ComparePage } from "./pages/ComparePage";
import { ExperimentPage } from "./pages/ExperimentPage";
import { ExperimentsPage } from "./pages/ExperimentsPage";
import { JudgePage } from "./pages/JudgePage";
import { LandingPage } from "./pages/LandingPage";
import { PolicyPage } from "./pages/PolicyPage";
import { ReviewPage } from "./pages/ReviewPage";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";
import { ScreeningPage } from "./pages/ScreeningPage";
import { TransferPage } from "./pages/TransferPage";

function NotFound() {
  return (
    <div className="page">
      <div className="state state-empty">
        <div className="state-title">Nothing at this address</div>
        <div className="state-action">
          <Link className="btn" to="/">
            New video
          </Link>
        </div>
      </div>
    </div>
  );
}

function ScrollToTop() {
  const { pathname } = useLocation();
  useEffect(() => { window.scrollTo({ top: 0, behavior: "instant" }); }, [pathname]);
  return null;
}

export default function App() {
  return (
    <BrowserRouter>
      <ScrollToTop />
      <SessionGate><AppStateProvider>
        <div className="shell">
          <TopBar />
          <MotionDirector />
          <ApiUnavailable />
          <main className="main" id="main">
            <Routes>
              <Route path="/" element={<LandingPage />} />
              <Route path="/judge/:videoId" element={<JudgePage />} />
              <Route path="/screen/:screenId" element={<ScreeningPage />} />
              <Route path="/compare/:abcId" element={<ComparePage />} />
              <Route path="/causal/:causalId" element={<CausalPage />} />
              <Route path="/runs" element={<RunsPage />} />
              <Route path="/runs/:runId" element={<RunPage />} />
              <Route path="/research" element={<Navigate to="/research/transfer" replace />} />
              <Route path="/research/transfer" element={<TransferPage />} />
              <Route path="/research/experiments" element={<ExperimentsPage />} />
              <Route path="/research/experiments/new/:videoId" element={<ExperimentPage />} />
              <Route path="/research/experiments/:id" element={<ExperimentPage />} />
              <Route path="/research/policy" element={<PolicyPage />} />
              <Route path="/research/corpus" element={<CorpusPage />} />
              <Route path="/research/review" element={<ReviewPage />} />
              <Route path="/transfer" element={<Navigate to="/research/transfer" replace />} />
              <Route path="/policy" element={<Navigate to="/research/policy" replace />} />
              <Route path="/experiments" element={<Navigate to="/research/experiments" replace />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </main>
        </div>
      </AppStateProvider></SessionGate>
    </BrowserRouter>
  );
}

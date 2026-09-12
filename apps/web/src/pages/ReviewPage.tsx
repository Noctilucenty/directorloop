import { useParams } from "react-router-dom";
import { getClient } from "../api/client";
import type { ReviewAssignment } from "../api/types";
import { HumanReviewPanel } from "../components/HumanReviewPanel";
import { ErrorState, Loading } from "../components/States";
import { useAsync } from "../hooks/useAsync";

export function ReviewPage() {
  const { token } = useParams();
  const state = useAsync<ReviewAssignment>(async () => (await getClient()).getReview(token ?? ""), [token]);
  if (state.loading) return <Loading what="review" />;
  if (state.error || !state.data || !token) {
    return <ErrorState title="This review link is not available" detail={state.error ?? undefined} onRetry={state.reload} />;
  }
  return (
    <div className="page page-narrow">
      <div className="page-head">
        <h1>Short video review</h1>
      </div>
      <HumanReviewPanel token={token} assignment={state.data} />
    </div>
  );
}

import React from "react";
import { render, screen } from "@testing-library/react";
import { SilenceBadge } from "@/features/silences/silence-badge";
import { SilenceMetadata } from "@/entities/silences/model";

describe("SilenceBadge", () => {
  it("renders Silenced badge when metadata.silenced is true", () => {
    const metadata: SilenceMetadata = {
      evaluated_at: "2026-10-04T12:00:00Z",
      silenced: true,
      coverage: "full",
      silenced_until: "2026-10-05T12:00:00Z",
      reasons: [
        {
          silence_id: "11111111-1111-1111-1111-111111111111",
          revision: 1,
          via: "fingerprint",
          incident_id: null,
          ends_at: "2026-10-05T12:00:00Z",
        },
      ],
      total_alerts: 1,
      silenced_alerts: 1,
    };

    render(<SilenceBadge metadata={metadata} />);
    expect(screen.getByText("Silenced")).toBeInTheDocument();
  });

  it("renders Partial Silence badge for incidents with partial coverage", () => {
    const metadata: SilenceMetadata = {
      evaluated_at: "2026-10-04T12:00:00Z",
      silenced: false,
      coverage: "partial",
      silenced_until: "2026-10-05T12:00:00Z",
      reasons: [],
      total_alerts: 5,
      silenced_alerts: 2,
    };

    render(<SilenceBadge metadata={metadata} isIncident={true} />);
    expect(screen.getByText("Silenced 2/5")).toBeInTheDocument();
  });

  it("renders Dismissed legacy badge when dismissed=true and no silence metadata", () => {
    render(<SilenceBadge dismissed={true} />);
    expect(screen.getByText("Dismissed")).toBeInTheDocument();
  });

  it("renders nothing when object is neither silenced nor dismissed", () => {
    const { container } = render(<SilenceBadge dismissed={false} metadata={null} />);
    expect(container.firstChild).toBeNull();
  });
});

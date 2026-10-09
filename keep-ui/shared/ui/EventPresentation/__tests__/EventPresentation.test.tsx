import { render, screen } from "@testing-library/react";
import { EventPresentation } from "../EventPresentation";
import { getIncidentName } from "@/entities/incidents/lib/utils";
import type { IncidentDto } from "@/entities/incidents/model";
import type { EventPresentation as Presentation } from "@/shared/lib/event-presentation";

test("shows collected objects on separate lines and boolean values", () => {
  const value: Presentation = { id: "engineer", config_digest: "fixture", title: "catalog", description: "Two affected pods",
    fields: [{ path: "incident.collections.objects", label: "Pods", value: "pod-a\npod-b", known: true, source: "labels.pod" },
      { path: "incident.flapping.active", label: "Flapping", value: false, known: true, source: null },
      { path: "incident.alerts_count", label: "Alerts", value: 0, known: true, source: null }],
    links: [], missing_fields: [] };
  render(<EventPresentation presentation={value} />);
  expect(screen.getByText(/pod-a[\s\S]*pod-b/).textContent).toBe("pod-a\npod-b");
  expect(screen.getByText("false")).toBeVisible();
  expect(screen.getByText("0")).toBeVisible();
});

const presentation: Presentation = {
  id: "object", config_digest: "fixture", title: "database: catalog", description: "<img src=x onerror=bad()> & cluster",
  fields: [{ path: "normalized.service", label: "Service", value: "catalog", known: true, source: "labels.app" },
    { path: "normalized.namespace", label: "Namespace", value: "unspecified", known: false }],
  links: [{ label: "Runbook", url: "https://runbooks.test/catalog" }], missing_fields: ["normalized.namespace"],
};

test("shows configured fields, source, incomplete data and a safe link", () => {
  render(<EventPresentation presentation={presentation} />);
  expect(screen.getByText("catalog")).toHaveAttribute("title", "labels.app");
  expect(screen.getByText("unspecified")).toHaveTextContent("(incomplete)");
  expect(screen.getByText(/Incomplete data:/)).toHaveTextContent("normalized.namespace");
  expect(screen.getByRole("link", { name: "Runbook" })).toHaveAttribute("rel", "noopener noreferrer");
});

test("source and generated description are rendered as text", () => {
  const { container } = render(<EventPresentation presentation={presentation} />);
  expect(screen.getByText(presentation.description)).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
});

test("manual name takes precedence and generated name works without AI", () => {
  const incident = { id: "id", generated_name: "workload: catalog", ai_generated_name: "old", user_generated_name: "" } as IncidentDto;
  expect(getIncidentName(incident)).toBe("workload: catalog");
  incident.user_generated_name = "Manual title";
  expect(getIncidentName(incident)).toBe("Manual title");
});

test("missing presentation is optional and unsafe link schemes are ignored", () => {
  const { container, rerender } = render(<EventPresentation />);
  expect(container).toBeEmptyDOMElement();
  rerender(<EventPresentation presentation={{ ...presentation, links: [{ label: "Unsafe", url: "javascript:bad()" }] }} showDescription={false} />);
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
  expect(screen.queryByText(presentation.description)).not.toBeInTheDocument();
});

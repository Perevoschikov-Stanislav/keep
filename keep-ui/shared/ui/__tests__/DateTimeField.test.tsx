import React from "react";
import { render, screen } from "@testing-library/react";
import {
  DateTimeField,
  formatUtcDateTime,
  parseDateSafe,
} from "../DateTimeField";

describe("DateTimeField", () => {
  describe("parseDateSafe", () => {
    it("handles Date instances", () => {
      const d = new Date("2026-10-06T12:00:00Z");
      expect(parseDateSafe(d)).toEqual(d);
    });

    it("handles ISO strings with Z", () => {
      const parsed = parseDateSafe("2026-10-06T12:00:00Z");
      expect(parsed?.toISOString()).toBe("2026-10-06T12:00:00.000Z");
    });

    it("handles ISO strings without Z by treating them as UTC", () => {
      const parsed = parseDateSafe("2026-10-06T12:00:00");
      expect(parsed?.toISOString()).toBe("2026-10-06T12:00:00.000Z");
    });

    it("handles space-separated datetime strings without timezone", () => {
      const parsed = parseDateSafe("2026-10-06 12:00:00");
      expect(parsed?.toISOString()).toBe("2026-10-06T12:00:00.000Z");
    });

    it("handles strings with timezone offset", () => {
      const parsed = parseDateSafe("2026-10-06T15:00:00+03:00");
      expect(parsed?.toISOString()).toBe("2026-10-06T12:00:00.000Z");
    });

    it("handles unix timestamps in milliseconds", () => {
      const ts = 1791288000000;
      const parsed = parseDateSafe(ts);
      expect(parsed?.getTime()).toBe(ts);
    });

    it("returns null for null, undefined, empty, or invalid input", () => {
      expect(parseDateSafe(null)).toBeNull();
      expect(parseDateSafe(undefined)).toBeNull();
      expect(parseDateSafe("")).toBeNull();
      expect(parseDateSafe("   ")).toBeNull();
      expect(parseDateSafe("invalid-date-string")).toBeNull();
      expect(parseDateSafe(new Date("invalid"))).toBeNull();
    });
  });

  describe("formatUtcDateTime", () => {
    it("formats strictly in UTC format 'dd MMM yy, HH:mm.ss UTC'", () => {
      const d = new Date("2026-10-06T12:30:45Z");
      expect(formatUtcDateTime(d)).toBe("06 Oct 26, 12:30.45 UTC");
    });

    it("pads single digits with zero", () => {
      const d = new Date("2026-01-05T04:08:09Z");
      expect(formatUtcDateTime(d)).toBe("05 Jan 26, 04:08.09 UTC");
    });
  });

  describe("DateTimeField component", () => {
    it("renders formatted UTC date and timeago for valid date", () => {
      render(<DateTimeField date="2026-10-06T12:30:45Z" />);
      expect(screen.getByText("06 Oct 26, 12:30.45 UTC")).toBeInTheDocument();
    });

    it("renders fallback dash for null/undefined date", () => {
      render(<DateTimeField date={null} />);
      expect(screen.getByText("-")).toBeInTheDocument();
    });
  });
});

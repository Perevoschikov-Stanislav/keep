import TimeAgo from "react-timeago";

const MONTH_NAMES = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

export function formatUtcDateTime(date: Date): string {
  const day = String(date.getUTCDate()).padStart(2, "0");
  const month = MONTH_NAMES[date.getUTCMonth()];
  const year = String(date.getUTCFullYear()).slice(-2);
  const hours = String(date.getUTCHours()).padStart(2, "0");
  const minutes = String(date.getUTCMinutes()).padStart(2, "0");
  const seconds = String(date.getUTCSeconds()).padStart(2, "0");
  return `${day} ${month} ${year}, ${hours}:${minutes}.${seconds} UTC`;
}

export function parseDateSafe(
  input: Date | string | number | null | undefined
): Date | null {
  if (input === null || input === undefined || input === "") return null;
  if (input instanceof Date) {
    return isNaN(input.getTime()) ? null : input;
  }
  if (typeof input === "number") {
    const d = new Date(input);
    return isNaN(d.getTime()) ? null : d;
  }
  if (typeof input === "string") {
    let trimmed = input.trim();
    if (!trimmed) return null;
    const hasTz = /([zZ]|[+-]\d{2}(?::?\d{2})?)$/.test(trimmed);
    if (!hasTz) {
      if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(trimmed)) {
        trimmed = trimmed.replace(" ", "T");
      }
      trimmed = `${trimmed}Z`;
    }
    const d = new Date(trimmed);
    return isNaN(d.getTime()) ? null : d;
  }
  return null;
}

export const DateTimeField = ({
  date,
}: {
  date: Date | string | number | null | undefined;
}) => {
  const parsed = parseDateSafe(date);
  if (!parsed) {
    return (
      <div>
        <p className="" suppressHydrationWarning>
          -
        </p>
      </div>
    );
  }

  return (
    <div>
      <p className="" suppressHydrationWarning>
        <TimeAgo date={parsed} suppressHydrationWarning />
      </p>
      <p className="text-gray-500 text-xs" suppressHydrationWarning>
        {formatUtcDateTime(parsed)}
      </p>
    </div>
  );
};

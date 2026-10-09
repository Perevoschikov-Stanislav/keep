import { AIPlugins } from "./ai-plugins";
import { notFound } from "next/navigation";

export default function Page() {
  if (process.env.KEEP_OSS_ONLY !== "false") notFound();
  return <AIPlugins />;
}

export const metadata = {
  title: "Keep - AI Correlation",
  description:
    "Correlate Alerts and Incidents with AI to identify patterns and trends.",
};

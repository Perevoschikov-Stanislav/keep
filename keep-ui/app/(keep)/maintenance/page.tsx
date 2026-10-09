import { redirect } from "next/navigation";

export default function Page() {
  redirect("/silences");
}

export const metadata = {
  title: "Keep - Silences",
  description: "Silence management registry",
};

import { redirect } from "next/navigation";

/** The friendly default destination is Chats; the old overview lives on. */
export default function RootPage() {
  redirect("/chats");
}

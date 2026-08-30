"use client";

/** The one tile every library card leads with. Four ways to fill it, tried in
 * order: the same-origin logo the icon proxy serves, the Lucide glyph the
 * curated catalog names, a monogram of the first letter, and finally a plug.
 * A logo that fails to load flips the tile to the next rung rather than
 * leaving a broken image, so an air-gapped install degrades to exactly what
 * the library showed before logos existed. */

import {
  BookOpen,
  Bug,
  Cable,
  Calendar,
  Cloud,
  Cpu,
  CreditCard,
  Database,
  Flame,
  FlaskConical,
  Folder,
  GitBranch,
  Globe,
  HardDrive,
  Kanban,
  LifeBuoy,
  ListTodo,
  Mail,
  MessageCircle,
  MessageSquare,
  Notebook,
  Palette,
  PenTool,
  Phone,
  Plug,
  Search,
  Send,
  Table,
  Terminal,
  Users,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";

/** Icon names the curated catalog uses, mapped to their Lucide glyphs. */
const ICONS: Record<string, LucideIcon> = {
  github: GitBranch,
  linear: Kanban,
  vercel: Cloud,
  terminal: Terminal,
  mcp: Cable,
  notebook: Notebook,
  "message-square": MessageSquare,
  "message-circle": MessageCircle,
  kanban: Kanban,
  "credit-card": CreditCard,
  users: Users,
  bug: Bug,
  cloud: Cloud,
  "life-buoy": LifeBuoy,
  zap: Zap,
  "check-square": ListTodo,
  palette: Palette,
  "pen-tool": PenTool,
  folder: Folder,
  calendar: Calendar,
  mail: Mail,
  table: Table,
  database: Database,
  globe: Globe,
  "hard-drive": HardDrive,
  search: Search,
  web: Search,
  flame: Flame,
  "book-open": BookOpen,
  send: Send,
  phone: Phone,
  cpu: Cpu,
  flask: FlaskConical,
};

/** The bare glyph, for the places that want an icon without the tile. */
export function AppIcon({ icon, size = 18 }: { icon: string; size?: number }) {
  const Icon = ICONS[icon] ?? Plug;
  return <Icon size={size} aria-hidden />;
}

const TILE_SIZES: Record<36 | 40 | 48, string> = {
  36: "h-9 w-9",
  40: "h-10 w-10",
  48: "h-12 w-12",
};

const MONOGRAM_SIZES: Record<36 | 40 | 48, string> = {
  36: "text-base",
  40: "text-lg",
  48: "text-xl",
};

export function LogoTile({
  name,
  icon,
  logoUrl,
  size = 40,
  className = "",
}: {
  name: string;
  icon?: string | null;
  logoUrl?: string | null;
  size?: 36 | 40 | 48;
  className?: string;
}) {
  // Remember which URL failed rather than a bare flag, so a re-render with a
  // different logo gets a fresh attempt instead of inheriting the old failure.
  const [failedSrc, setFailedSrc] = useState<string | null>(null);

  const IconGlyph = icon ? ICONS[icon] : undefined;
  const monogram = name.trim().charAt(0).toUpperCase();

  let content: React.ReactNode;
  if (logoUrl && logoUrl !== failedSrc) {
    content = (
      // Same-origin proxy bytes with their own caching headers; next/image
      // adds nothing here but a second proxy hop.
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={logoUrl}
        alt=""
        aria-hidden
        loading="lazy"
        className="h-full w-full object-contain"
        onError={() => setFailedSrc(logoUrl)}
      />
    );
  } else if (IconGlyph) {
    content = <IconGlyph size={size >= 48 ? 22 : 18} aria-hidden />;
  } else if (monogram) {
    content = (
      <span aria-hidden className={`font-display font-bold ${MONOGRAM_SIZES[size]}`}>
        {monogram}
      </span>
    );
  } else {
    content = <Plug size={size >= 48 ? 22 : 18} aria-hidden />;
  }

  return (
    <span
      className={`flex shrink-0 items-center justify-center overflow-hidden rounded-xl bg-accent-soft text-accent-strong ${TILE_SIZES[size]} ${className}`}
    >
      {content}
    </span>
  );
}

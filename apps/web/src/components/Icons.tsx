// Inline icons. Stroke-based, currentColor, 20px grid.
import type { ReactNode, SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Svg({ size = 18, children, ...rest }: IconProps & { children: ReactNode }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...rest}>
      {children}
    </svg>
  );
}

export const LinkIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M8.5 11.5a3 3 0 0 0 4.2 0l2.6-2.6a3 3 0 0 0-4.2-4.2l-1 1" />
    <path d="M11.5 8.5a3 3 0 0 0-4.2 0l-2.6 2.6a3 3 0 0 0 4.2 4.2l1-1" />
  </Svg>
);

export const UploadIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10 13V4" />
    <path d="M6.5 7.5 10 4l3.5 3.5" />
    <path d="M4 13.5V15a1.5 1.5 0 0 0 1.5 1.5h9A1.5 1.5 0 0 0 16 15v-1.5" />
  </Svg>
);

export const PlayIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6.5 4.5v11l9-5.5z" fill="currentColor" stroke="none" />
  </Svg>
);

export const PauseIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M7 4.5v11M13 4.5v11" strokeWidth={2.2} />
  </Svg>
);

export const ExternalIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M11 4h5v5" />
    <path d="M16 4 9 11" />
    <path d="M14 12v3a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h3" />
  </Svg>
);

export const CheckIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="m4.5 10.5 3.5 3.5 7.5-8" />
  </Svg>
);

export const CrossIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="m5.5 5.5 9 9M14.5 5.5l-9 9" />
  </Svg>
);

export const ChevronIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="m6 8 4 4 4-4" />
  </Svg>
);

export const FilmIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="4" y="3" width="12" height="14" rx="2" />
    <path d="M7 3v14M13 3v14M4 7h3M4 13h3M13 7h3M13 13h3" />
  </Svg>
);

export const SoundIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 8v4h3l4 3V5L7 8z" />
    <path d="M14 7.5a3.5 3.5 0 0 1 0 5" />
  </Svg>
);

export const LinkedIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="3" y="5" width="6" height="10" rx="1.5" />
    <rect x="11" y="5" width="6" height="10" rx="1.5" />
    <path d="M9 10h2" />
  </Svg>
);

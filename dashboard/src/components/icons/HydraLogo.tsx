'use client';

import { type SVGProps } from 'react';

/**
 * Hydra logo — three-headed serpent.
 *
 * Works at any size. Pass `className` for Tailwind styling.
 * Defaults to `currentColor` so it inherits text color.
 */
export function HydraLogo(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 64 64"
      fill="none"
      stroke="currentColor"
      strokeWidth="3"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {/* Body / trunk */}
      <path d="M32 58 C32 58 32 38 32 32" />

      {/* Center head */}
      <path d="M32 32 C32 24 32 16 32 10" />
      <path d="M28 14 L32 6 L36 14" />
      {/* Center eyes */}
      <circle cx="30" cy="12" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="34" cy="12" r="1.2" fill="currentColor" stroke="none" />

      {/* Left head */}
      <path d="M32 36 C26 32 18 26 14 18" />
      <path d="M10 22 L12 13 L18 20" />
      {/* Left eyes */}
      <circle cx="13" cy="17" r="1.2" fill="currentColor" stroke="none" />

      {/* Right head */}
      <path d="M32 36 C38 32 46 26 50 18" />
      <path d="M46 20 L52 13 L54 22" />
      {/* Right eyes */}
      <circle cx="51" cy="17" r="1.2" fill="currentColor" stroke="none" />

      {/* Forked tongues */}
      <path d="M31 6 L29 3 M33 6 L35 3" strokeWidth="1.5" />
      <path d="M11.5 13 L9 10.5 M13 13 L12 9.5" strokeWidth="1.5" />
      <path d="M52.5 13 L55 10.5 M51 13 L52 9.5" strokeWidth="1.5" />

      {/* Base coil */}
      <path d="M26 52 C22 54 22 58 28 58 L36 58 C42 58 42 54 38 52" />
    </svg>
  );
}

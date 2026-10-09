"use client";

import { Subtitle } from "@tremor/react";
import { LinkWithIcon } from "components/LinkWithIcon";
import { Session } from "next-auth";
import { Disclosure } from "@headlessui/react";
import { IoChevronUp } from "react-icons/io5";
import { useIncidents, usePollIncidents } from "utils/hooks/useIncidents";
import { MdFlashOn } from "react-icons/md";
import clsx from "clsx";
import { usePathname, useSearchParams } from "next/navigation";
import { IncidentView, useIncidentViews, combineIncidentCel } from "@/entities/incidents/model/useIncidentViews";
import {
  DEFAULT_INCIDENTS_PAGE_SIZE,
  DEFAULT_INCIDENTS_CEL,
  DEFAULT_INCIDENTS_SORTING,
} from "@/entities/incidents/model/models";

type IncidentsLinksProps = { session: Session | null };

function IncidentViewLink({ view }: { view: IncidentView }) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const selected = searchParams?.get("view") || "all";
  const active = pathname === "/incidents" && selected === view.id;
  const { data: incidents, mutate } = useIncidents(
    {
      candidate: false,
      predicted: null,
      limit: 0,
      offset: 0,
      sorting: DEFAULT_INCIDENTS_SORTING,
      cel: combineIncidentCel(DEFAULT_INCIDENTS_CEL, view.cel),
    },
    {}
  );
  usePollIncidents(mutate);
  return (
    <li>
      <LinkWithIcon
        href={view.id === "all" ? "/incidents" : `/incidents?view=${encodeURIComponent(view.id)}`}
        icon={MdFlashOn}
        count={incidents?.count}
        testId={`incident-view-${view.id}`}
        aria-current={active ? "page" : undefined}
        active={active}
        className={active ? "bg-stone-200/50" : undefined}
        isExact
      >
        <Subtitle className={clsx("text-xs", active && "!text-orange-400")}>{view.name}</Subtitle>
      </LinkWithIcon>
    </li>
  );
}

export const IncidentsLinks = ({ session }: IncidentsLinksProps) => {
  const isNOCRole = session?.userRole === "noc";
  const { views } = useIncidentViews();

  if (isNOCRole) {
    return null;
  }

  return (
    <Disclosure as="div" className="space-y-0.5" defaultOpen>
      <Disclosure.Button className="w-full flex justify-between items-center px-2">
        {({ open }) => (
          <>
            <Subtitle className="text-xs ml-2 text-gray-900 font-medium uppercase">
              INCIDENTS
            </Subtitle>
            <IoChevronUp
              className={clsx({ "rotate-180": open }, "mr-2 text-slate-400")}
            />
          </>
        )}
      </Disclosure.Button>

      <Disclosure.Panel as="ul" className="space-y-0.5 p-1 pr-1">
        {views.map((view) => <IncidentViewLink key={view.id} view={view} />)}
      </Disclosure.Panel>
    </Disclosure>
  );
};

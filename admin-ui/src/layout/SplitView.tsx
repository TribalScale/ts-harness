import React from "react";
import { Icon } from "@astryxdesign/core/Icon";
import { Button } from "../components/Button";
import { useMediaQuery } from "./useMediaQuery";

export function SplitView({
  list,
  detail,
  detailOpen,
  onCloseDetail,
  className,
}: {
  list: React.ReactNode;
  detail: React.ReactNode;
  detailOpen: boolean;
  onCloseDetail: () => void;
  className?: string;
}) {
  const desktop = useMediaQuery("(min-width: 1024px)");
  const splitClass = className ? `split ${className}` : "split";

  if (desktop) {
    return (
      <div className={splitClass}>
        <aside className="split-list" aria-label="Items">
          {list}
        </aside>
        <section className="split-detail">{detail}</section>
      </div>
    );
  }

  return (
    <div className={splitClass}>
      <aside className="split-list" aria-label="Items">
        {list}
      </aside>
      {detailOpen && (
        <>
          <div className="slide-backdrop" onClick={onCloseDetail} />
          <section className="slide-panel" aria-label="Item detail">
            <Button
              variant="secondary"
              size="sm"
              className="slide-close"
              type="button"
              isIconOnly
              icon={<Icon icon="close" />}
              aria-label="Close"
              onClick={onCloseDetail}
            />
            {detail}
          </section>
        </>
      )}
    </div>
  );
}

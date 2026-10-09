"use client";

import { useCallback } from "react";
import { signOut } from "next-auth/react";
import * as Sentry from "@sentry/nextjs";
import posthog from "posthog-js";
import { useConfig } from "@/utils/hooks/useConfig";
import { AuthType } from "@/utils/authenticationType";
import { OAUTH2PROXY_SIGN_OUT_URL } from "@/shared/lib/oauth2proxy-logout";

export function useSignOut() {
  const { data: configData } = useConfig();

  return useCallback(async () => {
    if (!configData) {
      return;
    }

    if (configData?.SENTRY_DISABLED !== "true") {
      Sentry.setUser(null);
    }

    if (configData?.POSTHOG_DISABLED !== "true") {
      posthog.reset();
    }

    // For OAUTH2PROXY auth, redirect to oauth2-proxy's sign_out endpoint
    // This properly clears the oauth2-proxy session
    if (configData?.AUTH_TYPE === AuthType.OAUTH2PROXY) {
      await signOut({ redirect: false });
      window.location.href = OAUTH2PROXY_SIGN_OUT_URL;
      return;
    }

    signOut();
  }, [configData]);
}

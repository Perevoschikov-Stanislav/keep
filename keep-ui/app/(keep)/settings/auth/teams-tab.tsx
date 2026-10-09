"use client";

import {
  Badge,
  Card,
  Subtitle,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
  Text,
  Title,
} from "@tremor/react";
import Loading from "@/app/(keep)/loading";
import { useTeams } from "@/utils/hooks/useTeams";

export default function TeamsTab() {
  const { data, error, isLoading } = useTeams();

  if (error) {
    return (
      <div role="alert">
        <Text>Unable to load teams. Try refreshing the page.</Text>
      </div>
    );
  }
  if (isLoading || !data) return <Loading />;

  return (
    <div className="h-full flex flex-col gap-4">
      <div>
        <Title>Teams</Title>
        <Subtitle>
          Teams and membership groups are managed through IaC. Only teams
          available to you are shown.
        </Subtitle>
      </div>
      {data.configuration && (
        <Card>
          <Title>Active IaC configuration</Title>
          <Text>
            Revision: {data.configuration.revision} · Generation: {data.configuration.generation}
          </Text>
          <Text className="break-all">Digest: {data.configuration.digest}</Text>
          {data.configuration.source && <Text>Source: {data.configuration.source}</Text>}
          {data.configuration.applied_by && (
            <Text>Applied by {data.configuration.applied_by} at {data.configuration.applied_at}</Text>
          )}
          {data.configuration.result && <Text>Last apply: {data.configuration.result}</Text>}
          {data.configuration.drift_count != null && (
            <Text>Resources with drift: {data.configuration.drift_count}</Text>
          )}
        </Card>
      )}
      {!data.enabled ? (
        <Text>Team isolation is not configured.</Text>
      ) : (
        <>
          <Text>
            {data.visibility === "all"
              ? "Shared visibility: all teams' events are readable. Responders can change only their own teams' events."
              : "Team visibility: events are readable by members of the teams listed under Visible to. Only accessible team IDs are shown."}
          </Text>
          {data.teams.length === 0 ? (
            <Text>No teams are available to your account.</Text>
          ) : (
            <Card className="overflow-auto p-0">
              <Table>
                <TableHead>
                  <TableRow>
                    <TableHeaderCell>Team</TableHeaderCell>
                    <TableHeaderCell>Membership groups</TableHeaderCell>
                    <TableHeaderCell>Event zones</TableHeaderCell>
                    <TableHeaderCell>Visible to</TableHeaderCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {data.teams.map((team) => (
                    <TableRow key={team.id}>
                      <TableCell>{team.id}</TableCell>
                      {[
                        team.groups,
                        team.zones,
                        data.visibility === "all"
                          ? ["All roles"]
                          : team.visible_to,
                      ].map((values, index) => (
                        <TableCell key={index}>
                          <div className="flex flex-wrap gap-1">
                            {values.length === 0
                              ? "—"
                              : values.map((value) => (
                                  <Badge key={value} color="orange">
                                    {value}
                                  </Badge>
                                ))}
                          </div>
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

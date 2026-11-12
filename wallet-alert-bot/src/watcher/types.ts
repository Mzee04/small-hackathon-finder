export type ActivityKind =
  | "native_transfer"
  | "erc20_transfer"
  | "nft_transfer"
  | "approval"
  | "contract_call"
  | "unknown";

export type DetectedActivity = {
  chainId: string;
  chainName: string;
  txHash: string;
  blockNumber: number;
  wallet: string;
  kind: ActivityKind;
  summary: string;
  explorerUrl: string;
};

{{/*
Node IAM role name — explicit override or KarpenterNodeRole-<cluster> (created
by the platform `karpenter` addon).
*/}}
{{- define "kata-nodepool.roleName" -}}
{{- .Values.defaults.roleName | default (printf "KarpenterNodeRole-%s" .Values.aws.clusterName) -}}
{{- end -}}

{{/*
Subnet discovery — PRIVATE subnets of this cluster (cluster tag + internal-elb
role tag). No hardcoded IDs.
*/}}
{{- define "kata-nodepool.subnetSelector" -}}
subnetSelectorTerms:
  - tags:
      {{ .Values.discovery.subnetClusterTagKey }}: {{ .Values.aws.clusterName | quote }}
      {{ .Values.discovery.subnetPrivateTag.key }}: {{ .Values.discovery.subnetPrivateTag.value | quote }}
{{- end -}}

{{/*
Security-group discovery — the EKS-managed cluster SG.
*/}}
{{- define "kata-nodepool.sgSelector" -}}
securityGroupSelectorTerms:
  - tags:
      {{ .Values.discovery.securityGroupClusterTagKey }}: {{ .Values.aws.clusterName | quote }}
{{- end -}}

{{/*
Common EC2NodeClass tail (block device, IMDS). Indented for spec:.
*/}}
{{- define "kata-nodepool.ec2common" -}}
blockDeviceMappings:
  - deviceName: /dev/xvda
    ebs:
      volumeSize: {{ .Values.defaults.volumeSizeGi }}Gi
      volumeType: {{ .Values.defaults.volumeType }}
      deleteOnTermination: true
      encrypted: true
metadataOptions:
  httpEndpoint: enabled
  httpTokens: required
{{- end -}}

{{/*
userData for the KATA pools (clh/qemu): load kvm_intel so /dev/kvm exists, then
nodeadm joins. Takes the root context.
*/}}
{{- define "kata-nodepool.userData.kata" -}}
userData: |
  MIME-Version: 1.0
  Content-Type: multipart/mixed; boundary="//"

  --//
  Content-Type: text/x-shellscript; charset="us-ascii"

  #!/bin/bash
  modprobe kvm_intel
  printf 'kvm\nkvm_intel\n' > /etc/modules-load.d/kvm.conf

  --//
  Content-Type: application/node.eks.aws

  apiVersion: node.eks.aws/v1alpha1
  kind: NodeConfig
  spec:
    cluster:
      name: {{ .Values.aws.clusterName }}
  --//--
{{- end -}}

{{/*
userData for the FIRECRACKER pools: kvm + a loop-backed devmapper thin-pool +
the containerd devmapper snapshotter (fc needs a block snapshotter, not
overlayfs; kata-deploy-fc adds only the runtimes.kata-fc handler). Takes root ctx.
UNVERIFIED on this platform — ported from openclaw.
*/}}
{{- define "kata-nodepool.userData.fc" -}}
userData: |
  MIME-Version: 1.0
  Content-Type: multipart/mixed; boundary="//"

  --//
  Content-Type: text/x-shellscript; charset="us-ascii"

  #!/bin/bash
  set -euxo pipefail
  exec >/var/log/kata-fc-userdata.log 2>&1

  # 1. Nested/native KVM → /dev/kvm.
  modprobe kvm_intel
  printf 'kvm\nkvm_intel\n' > /etc/modules-load.d/kvm.conf

  # 2. devmapper thin-pool "devpool" the containerd snapshotter attaches to.
  modprobe dm_thin_pool
  printf 'dm_thin_pool\n' > /etc/modules-load.d/devmapper.conf
  dnf install -y device-mapper-persistent-data || true

  DATA_DIR=/var/lib/containerd/devmapper
  mkdir -p "${DATA_DIR}"
  DATA_FILE="${DATA_DIR}/data"; META_FILE="${DATA_DIR}/meta"
  if ! dmsetup info devpool >/dev/null 2>&1; then
    [ -f "${DATA_FILE}" ] || { touch "${DATA_FILE}" && truncate -s {{ .Values.defaults.devmapper.dataSizeGi }}G "${DATA_FILE}"; }
    [ -f "${META_FILE}" ] || { touch "${META_FILE}" && truncate -s {{ .Values.defaults.devmapper.metaSizeGi }}G  "${META_FILE}"; }
    DATA_DEV=$(losetup --find --show "${DATA_FILE}")
    META_DEV=$(losetup --find --show "${META_FILE}")
    LENGTH_SECTORS=$(( $(blockdev --getsize64 -q "${DATA_DEV}") / 512 ))
    dmsetup create devpool \
      --table "0 ${LENGTH_SECTORS} thin-pool ${META_DEV} ${DATA_DEV} 128 32768"
  fi

  --//
  Content-Type: application/node.eks.aws

  apiVersion: node.eks.aws/v1alpha1
  kind: NodeConfig
  spec:
    cluster:
      name: {{ .Values.aws.clusterName }}
    containerd:
      config: |
        [plugins."io.containerd.grpc.v1.cri".containerd]
          discard_unpacked_layers = false
        [plugins."io.containerd.snapshotter.v1.devmapper"]
          pool_name = "devpool"
          root_path = "/var/lib/containerd/devmapper"
          base_image_size = "{{ .Values.defaults.devmapper.baseImageSize }}"
          discard_blocks = true
  --//--
{{- end -}}

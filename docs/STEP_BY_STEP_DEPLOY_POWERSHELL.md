# Step-by-Step AWS Deployment Guide (Windows PowerShell)

**Account:** `685057748560` | **Region:** `us-east-1` | **Parser:** Ollama

This guide is the single source of truth for deploying the MultiModal RAG pipeline to AWS using **Windows PowerShell**.

---

## 1. Prerequisites
```powershell
aws --version     # Expected: aws-cli/2.x.x
docker --version  # Expected: Docker version 24.x or higher
winget install jqlang.jq
winget install --id GitHub.cli
gh auth login
```

## 2. IAM — Create Admin User
1. AWS Console → IAM → Users → Create user (`doc-parser-admin`).
2. Attach policy directly: **`AdministratorAccess`**.
3. Create **Command Line Interface (CLI)** access key.

```powershell
aws configure --profile doc-parser-admin
$env:AWS_PROFILE="doc-parser-admin"
aws sts get-caller-identity
```

## 3. Shell Variables
Run these at the start of every terminal session.

```powershell
$AWS_REGION = "us-east-1"
$AWS_ACCOUNT_ID = aws sts get-caller-identity --query Account --output text
$ECR_REGISTRY = "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
$CLUSTER_NAME = "doc-parser-cluster"

# Retrieve default VPC and Subnets
$VPC_ID = aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" --query "Vpcs.VpcId" --output text
$SUBNET_IDS = (aws ec2 describe-subnets --filters "Name=defaultForAz,Values=true" --query "Subnets[*].SubnetId" --output json) | ConvertFrom-Json
$SUBNET1 = $SUBNET_IDS
$SUBNET2 = $SUBNET_IDS

Write-Host "Account: $AWS_ACCOUNT_ID | VPC: $VPC_ID | Subnets: $SUBNET1, $SUBNET2"
```

## 4. Security Groups

```powershell
# ALB Security Group
$ALB_SG = aws ec2 create-security-group `
  --group-name doc-parser-alb-sg `
  --description "ALB for doc-parser" `
  --vpc-id $VPC_ID `
  --query "GroupId" --output text

aws ec2 authorize-security-group-ingress `
  --group-id $ALB_SG `
  --protocol tcp --port 80 --cidr 0.0.0.0/0

# ECS Security Group
$ECS_SG = aws ec2 create-security-group `
  --group-name doc-parser-ecs-sg `
  --description "ECS tasks for doc-parser" `
  --vpc-id $VPC_ID `
  --query "GroupId" --output text

# Allow ALB to reach ECS tasks on port 8000
aws ec2 authorize-security-group-ingress `
  --group-id $ECS_SG `
  --protocol tcp --port 8000 --source-group $ALB_SG

# Allow EFS mount traffic within ECS tasks
aws ec2 authorize-security-group-ingress `
  --group-id $ECS_SG `
  --protocol tcp --port 2049 --source-group $ECS_SG
```

## 5. ECR Repositories
```powershell
aws ecr create-repository --repository-name doc-parser/app --region $AWS_REGION --image-scanning-configuration scanOnPush=true
```

## 6. ECS Cluster
```powershell
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com
aws ecs create-cluster --cluster-name $CLUSTER_NAME --capacity-providers FARGATE FARGATE_SPOT --region $AWS_REGION
```

## 7. EFS — Persistent Storage
```powershell
$FS_ID = aws efs create-file-system --performance-mode generalPurpose --throughput-mode bursting --region $AWS_REGION --query "FileSystemId" --output text
Write-Host "EFS ID: $FS_ID"

# Wait until available (run until it says available)
aws efs describe-file-systems --file-system-id $FS_ID --query "FileSystems.LifeCycleState" --output text

aws efs create-mount-target --file-system-id $FS_ID --subnet-id $SUBNET1 --security-groups $ECS_SG
aws efs create-mount-target --file-system-id $FS_ID --subnet-id $SUBNET2 --security-groups $ECS_SG

$QDRANT_AP = aws efs create-access-point --file-system-id $FS_ID --posix-user Uid=1000,Gid=1000 --root-directory "Path=/qdrant,CreationInfo={OwnerUid=1000,OwnerGid=1000,Permissions=755}" --query "AccessPointId" --output text
$OLLAMA_AP = aws efs create-access-point --file-system-id $FS_ID --posix-user Uid=0,Gid=0 --root-directory "Path=/ollama,CreationInfo={OwnerUid=0,OwnerGid=0,Permissions=755}" --query "AccessPointId" --output text
```

## 8. Secrets Manager
PowerShell strips quotes from inline JSON, so we use a temporary file to guarantee valid JSON formatting.

```powershell
'{"OPENAI_API_KEY":"sk-YOUR-KEY-HERE"}' | Out-File secret.json -Encoding ascii

aws secretsmanager create-secret `
  --name doc-parser/openai-api-key `
  --secret-string file://secret.json `
  --region $AWS_REGION

Remove-Item secret.json
```

## 9. IAM — CI/CD Bot User
```powershell
aws iam create-user --user-name doc-parser-cicd
aws iam create-access-key --user-name doc-parser-cicd # SAVE THIS OUTPUT

@"
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*" },
    { "Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"], "Resource": ["arn:aws:ecr:*:*:repository/doc-parser/app"] },
    { "Effect": "Allow", "Action": ["ecs:UpdateService", "ecs:DescribeServices"], "Resource": "*" },
    { "Effect": "Allow", "Action": ["ecs:DescribeTaskDefinition", "ecs:ListTasks", "ecs:DescribeTasks"], "Resource": "*" }
  ]
}
"@ | Out-File -FilePath cicd-policy.json -Encoding ascii

aws iam put-user-policy --user-name doc-parser-cicd --policy-name doc-parser-cicd-policy --policy-document file://cicd-policy.json
```

## 10. IAM — ECS Task Execution Role
```powershell
@"
{"Version": "2012-10-17","Statement": [{"Effect": "Allow","Principal": {"Service": "ecs-tasks.amazonaws.com"},"Action": "sts:AssumeRole"}]}
"@ | Out-File trust-policy.json -Encoding ascii

aws iam create-role --role-name doc-parser-ecs-task-execution --assume-role-policy-document file://trust-policy.json

aws iam attach-role-policy --role-name doc-parser-ecs-task-execution --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

@"
{"Version": "2012-10-17","Statement": [{"Effect": "Allow","Action": ["secretsmanager:GetSecretValue"],"Resource": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:doc-parser/*"}]}
"@ | Out-File secrets-policy.json -Encoding ascii
aws iam put-role-policy --role-name doc-parser-ecs-task-execution --policy-name secrets-manager-read --policy-document file://secrets-policy.json

@"
{"Version": "2012-10-17","Statement": [{"Effect": "Allow","Action": ["elasticfilesystem:ClientMount","elasticfilesystem:ClientWrite","elasticfilesystem:DescribeMountTargets"],"Resource": "arn:aws:elasticfilesystem:${AWS_REGION}:${AWS_ACCOUNT_ID}:file-system/${FS_ID}"}]}
"@ | Out-File efs-policy.json -Encoding ascii
aws iam put-role-policy --role-name doc-parser-ecs-task-execution --policy-name efs-mount --policy-document file://efs-policy.json

@"
{"Version": "2012-10-17","Statement": [{"Effect": "Allow","Action": ["ssmmessages:CreateControlChannel","ssmmessages:CreateDataChannel","ssmmessages:OpenControlChannel","ssmmessages:OpenDataChannel"],"Resource": "*"}]}
"@ | Out-File exec-policy.json -Encoding ascii
aws iam put-role-policy --role-name doc-parser-ecs-task-execution --policy-name ecs-exec --policy-document file://exec-policy.json

$EXECUTION_ROLE_ARN = "arn:aws:iam::${AWS_ACCOUNT_ID}:role/doc-parser-ecs-task-execution"
```

## 11. CloudWatch Log Groups
```powershell
aws logs create-log-group --log-group-name /ecs/doc-parser-app --region $AWS_REGION
```

## 12. ECS Task Definitions
Create `app-task-def.json` using the local template, replace placeholders, and register it.

```powershell
$content = Get-Content app-task-def.json
$content = $content -replace "arn:aws:iam::\d+:role/doc-parser-ecs-task-execution", $EXECUTION_ROLE_ARN
$content = $content -replace "fs-07c043a931acaa4dc", $FS_ID
$content = $content -replace "fsap-06687cf2cda64c1a6", $QDRANT_AP
$content = $content -replace "fsap-07b78e3e55c0dd911", $OLLAMA_AP
$content = $content -replace "\d+\.dkr\.ecr\..*\.amazonaws\.com", $ECR_REGISTRY
$content = $content -replace "arn:aws:secretsmanager:.*:secret", "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret"
$content | Set-Content app-task-def.json

aws ecs register-task-definition --cli-input-json file://app-task-def.json --region $AWS_REGION
```

## 13. Application Load Balancer
```powershell
$ALB_ARN = aws elbv2 create-load-balancer --name doc-parser-alb --subnets $SUBNET1 $SUBNET2 --security-groups $ALB_SG --scheme internet-facing --type application --query "LoadBalancers[0].LoadBalancerArn" --output text

$APP_TG_ARN = aws elbv2 create-target-group --name doc-parser-app-tg --protocol HTTP --port 8000 --target-type ip --vpc-id $VPC_ID --health-check-path /health --query "TargetGroups[0].TargetGroupArn" --output text

$LISTENER_ARN = aws elbv2 create-listener --load-balancer-arn $ALB_ARN --protocol HTTP --port 80 --default-actions Type=forward,TargetGroupArn=$APP_TG_ARN --query "Listeners[0].ListenerArn" --output text

aws elbv2 modify-load-balancer-attributes --load-balancer-arn $ALB_ARN --attributes Key=idle_timeout.timeout_seconds,Value=300 --region $AWS_REGION

$ALB_DNS = aws elbv2 describe-load-balancers --load-balancer-arns $ALB_ARN --query "LoadBalancers[0].DNSName" --output text
Write-Host "Public URL: http://${ALB_DNS}"
```

## 14. ECS Services
```powershell
aws ecs create-service `
  --cluster $CLUSTER_NAME `
  --service-name doc-parser-app `
  --task-definition doc-parser-app `
  --desired-count 1 `
  --launch-type FARGATE `
  --enable-execute-command `
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET1,$SUBNET2],securityGroups=[$ECS_SG],assignPublicIp=ENABLED}" `
  --load-balancers "targetGroupArn=$APP_TG_ARN,containerName=app,containerPort=8000" `
  --region $AWS_REGION

aws ecs wait services-stable --cluster $CLUSTER_NAME --services doc-parser-app
Write-Host "Service is stable."
```

## 15. Ollama Model Bootstrap
Run this **once** to download the model into your persistent EFS volume.
```powershell
$TASK_ARN = aws ecs list-tasks --cluster $CLUSTER_NAME --service-name doc-parser-app --query "taskArns[0]" --output text

aws ecs execute-command `
  --cluster $CLUSTER_NAME `
  --task $TASK_ARN `
  --container ollama `
  --interactive `
  --command "ollama pull glm4v:9b"
```

## 16. GitHub Actions Secrets
Set these in your repository settings using the GitHub CLI:
```powershell
gh secret set AWS_ACCESS_KEY_ID     --body "<cicd-access-key-id>"
gh secret set AWS_SECRET_ACCESS_KEY --body "<cicd-secret-access-key>"
gh secret set AWS_REGION            --body $AWS_REGION
gh secret set ECR_REGISTRY          --body $ECR_REGISTRY
gh secret set ECS_CLUSTER           --body $CLUSTER_NAME
gh secret set ECS_SERVICE_APP       --body "doc-parser-app"
```

## 17. Teardown & Rollback
**Stop the infrastructure to save money:**
```powershell
aws ecs update-service --cluster $CLUSTER_NAME --service doc-parser-app --desired-count 0 --region $AWS_REGION
aws elbv2 delete-listener --listener-arn $LISTENER_ARN --region $AWS_REGION
aws elbv2 delete-load-balancer --load-balancer-arn $ALB_ARN --region $AWS_REGION
```

**Full Teardown (Irreversible):**
```powershell
aws ecs delete-service --cluster $CLUSTER_NAME --service doc-parser-app --force --region $AWS_REGION

$APS = aws efs describe-access-points --file-system-id $FS_ID --query "AccessPoints[*].AccessPointId" --output text
if ($APS) { $APS -split "`t" | ForEach-Object { aws efs delete-access-point --access-point-id $_ } }

$MTS = aws efs describe-mount-targets --file-system-id $FS_ID --query "MountTargets[*].MountTargetId" --output text
if ($MTS) { $MTS -split "`t" | ForEach-Object { aws efs delete-mount-target --mount-target-id $_ } }

Start-Sleep -Seconds 45
aws efs delete-file-system --file-system-id $FS_ID --region $AWS_REGION
aws ecs delete-cluster --cluster $CLUSTER_NAME --region $AWS_REGION
aws ecr delete-repository --repository-name doc-parser/app --force --region $AWS_REGION
```
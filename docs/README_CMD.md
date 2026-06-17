# Step-by-Step AWS Deployment Guide (Windows Command Prompt)

**Account:** `685057748560` | **Region:** `us-east-1` | **Parser:** Ollama (local, no Z.AI key)

This guide is the single source of truth for deploying the MultiModal RAG pipeline to AWS from a completely fresh account using **Windows CMD**. Follow the phases in order — each phase depends on the one before it.

---

## Table of Contents
1. Prerequisites
2. IAM — Create Admin User
3. Shell Variables
4. Security Groups
5. ECR Repositories
6. ECS Cluster
7. EFS — Persistent Storage
8. Secrets Manager
9. IAM — CI/CD Bot User
10. IAM — ECS Task Execution Role
11. CloudWatch Log Groups
12. ECS Task Definitions
13. Application Load Balancer
14. ECS Services
15. Ollama Model Bootstrap
16. GitHub Actions Secrets
17. Verify Deployment
18. Troubleshooting Common Deployment Issues
19. CI/CD Flow Reference
20. Rollback Procedure
21. Cost Overview
22. How to Stop the Infrastructure (Save Money)
23. How to Restart the Infrastructure
24. How to Tear Down Everything (Full Deletion)

---

## 1. Prerequisites

Install Docker Desktop, AWS CLI v2, and GitHub CLI, then run:
```cmd
gh auth login
```

---

## 2. IAM — Create Admin User

This step is done **once via the AWS Console** using your root account. After this, you never use root credentials again.

1. AWS Console → **IAM** → **Users** → **Create user** (`doc-parser-admin`).
2. Do **not** enable console access (CLI only).
3. Choose **"Attach policies directly"** → check **`AdministratorAccess`**.
4. Open the user → **Security credentials** tab → **Create access key** (Command Line Interface).
5. **Save the Access Key ID and Secret Access Key**.

### Configure AWS CLI
```cmd
aws configure --profile doc-parser-admin
:: AWS Access Key ID:     <paste key id>
:: AWS Secret Access Key: <paste secret key>
:: Default region name:   us-east-1
:: Default output format: json

set AWS_PROFILE=doc-parser-admin
aws sts get-caller-identity
:: Expected: "Arn": "arn:aws:iam::685057748560:user/doc-parser-admin"
```

---

## 3. Shell Variables

Run these at the start of every terminal session before executing any commands in this guide.

```cmd
FOR /F "tokens=*" %i IN ('aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" --query "Vpcs[0].VpcId" --output text --region us-east-1') DO set VPC_ID=%i

:: Manually list your subnets, then set them below
aws ec2 describe-subnets --filters "Name=defaultForAz,Values=true" --query "Subnets[*].{SubnetId:SubnetId,AZ:AvailabilityZone}" --output table --region us-east-1

set SUBNET1=subnet-XXXXXXXX
set SUBNET2=subnet-YYYYYYYY

set AWS_REGION=us-east-1
set AWS_ACCOUNT_ID=685057748560
set ECR_REGISTRY=%AWS_ACCOUNT_ID%.dkr.ecr.%AWS_REGION%.amazonaws.com
set CLUSTER_NAME=doc-parser-cluster

echo Account: %AWS_ACCOUNT_ID% | Region: %AWS_REGION% | VPC: %VPC_ID% | Subnets: %SUBNET1%, %SUBNET2%
```

---

## 4. Security Groups

Two security groups are needed: ALB SG (faces the internet) and ECS SG (faces the ALB).

```cmd
:: --- ALB Security Group ---
FOR /F "tokens=*" %i IN ('aws ec2 create-security-group --group-name doc-parser-alb-sg --description "ALB for doc-parser" --vpc-id %VPC_ID% --query "GroupId" --output text') DO set ALB_SG=%i
echo ALB SG: %ALB_SG%

aws ec2 authorize-security-group-ingress ^
  --group-id %ALB_SG% ^
  --protocol tcp --port 80 --cidr 0.0.0.0/0

:: --- ECS Security Group ---
FOR /F "tokens=*" %i IN ('aws ec2 create-security-group --group-name doc-parser-ecs-sg --description "ECS tasks for doc-parser" --vpc-id %VPC_ID% --query "GroupId" --output text') DO set ECS_SG=%i
echo ECS SG: %ECS_SG%

:: Allow ALB → ECS on FastAPI port
aws ec2 authorize-security-group-ingress ^
  --group-id %ECS_SG% ^
  --protocol tcp --port 8000 --source-group %ALB_SG%

:: Allow EFS mount traffic within ECS tasks
aws ec2 authorize-security-group-ingress ^
  --group-id %ECS_SG% ^
  --protocol tcp --port 2049 --source-group %ECS_SG%
```

> **Save these values** — you will need them in later phases.

---

## 5. ECR Repositories

Container images are stored in ECR. One repository for the FastAPI app.

```cmd
aws ecr create-repository ^
  --repository-name doc-parser/app ^
  --region %AWS_REGION% ^
  --image-scanning-configuration scanOnPush=true
```

---

## 6. ECS Cluster

```cmd
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com

aws ecs create-cluster ^
  --cluster-name %CLUSTER_NAME% ^
  --capacity-providers FARGATE FARGATE_SPOT ^
  --region %AWS_REGION%
```

---

## 7. EFS — Persistent Storage

EFS provides two persistent volumes that survive deployments:
- `/qdrant/storage` — Qdrant vector database data
- `/root/.ollama` — Ollama model weights

```cmd
:: Create the file system
FOR /F "tokens=*" %i IN ('aws efs create-file-system --performance-mode generalPurpose --throughput-mode bursting --region %AWS_REGION% --query "FileSystemId" --output text') DO set FS_ID=%i
echo EFS ID: %FS_ID%

:: Wait until available
aws efs describe-file-systems --file-system-id %FS_ID% --query "FileSystems[0].LifeCycleState" --output text

:: Create mount targets
aws efs create-mount-target --file-system-id %FS_ID% --subnet-id %SUBNET1% --security-groups %ECS_SG%
aws efs create-mount-target --file-system-id %FS_ID% --subnet-id %SUBNET2% --security-groups %ECS_SG%

:: Access point for Qdrant data
FOR /F "tokens=*" %i IN ('aws efs create-access-point --file-system-id %FS_ID% --posix-user Uid=1000,Gid=1000 --root-directory "Path=/qdrant,CreationInfo={OwnerUid=1000,OwnerGid=1000,Permissions=755}" --query "AccessPointId" --output text') DO set QDRANT_AP=%i

:: Access point for Ollama model weights
FOR /F "tokens=*" %i IN ('aws efs create-access-point --file-system-id %FS_ID% --posix-user Uid=0,Gid=0 --root-directory "Path=/ollama,CreationInfo={OwnerUid=0,OwnerGid=0,Permissions=755}" --query "AccessPointId" --output text') DO set OLLAMA_AP=%i
```

---

## 8. Secrets Manager

Only `OPENAI_API_KEY` is needed. CMD strips quotes from inline JSON, so we use a temporary file.

```cmd
echo {"OPENAI_API_KEY":"sk-YOUR-KEY-HERE"} > secret.json

aws secretsmanager create-secret ^
  --name doc-parser/openai-api-key ^
  --secret-string file://secret.json ^
  --region %AWS_REGION%

del secret.json
```

---

## 9. IAM — CI/CD Bot User

This is the machine user whose credentials go into GitHub Actions.

```cmd
aws iam create-user --user-name doc-parser-cicd
aws iam create-access-key --user-name doc-parser-cicd 
:: SAVE THE OUTPUT ABOVE

:: Create cicd-policy.json manually locally using Notepad containing the exact policy JSON from the main guide, then:
aws iam put-user-policy ^
  --user-name doc-parser-cicd ^
  --policy-name doc-parser-cicd-policy ^
  --policy-document file://cicd-policy.json
```

---

## 10. IAM — ECS Task Execution Role

This is an IAM Role that Fargate assumes at runtime to pull images, write logs, read secrets, and mount EFS.

```cmd
:: Create trust-policy.json manually, then:
aws iam create-role --role-name doc-parser-ecs-task-execution --assume-role-policy-document file://trust-policy.json

aws iam attach-role-policy ^
  --role-name doc-parser-ecs-task-execution ^
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

:: Create secrets-policy.json, efs-policy.json, exec-policy.json manually, then:
aws iam put-role-policy --role-name doc-parser-ecs-task-execution --policy-name secrets-manager-read --policy-document file://secrets-policy.json
aws iam put-role-policy --role-name doc-parser-ecs-task-execution --policy-name efs-mount --policy-document file://efs-policy.json
aws iam put-role-policy --role-name doc-parser-ecs-task-execution --policy-name ecs-exec --policy-document file://exec-policy.json

set EXECUTION_ROLE_ARN=arn:aws:iam::%AWS_ACCOUNT_ID%:role/doc-parser-ecs-task-execution
```

---

## 11. CloudWatch Log Groups

```cmd
aws logs create-log-group ^
  --log-group-name /ecs/doc-parser-app ^
  --region %AWS_REGION%
```

---

## 12. ECS Task Definitions

Open `app-task-def.json` (from the project root) in a text editor like Notepad. Manually replace all placeholders with the values you generated above (`EXECUTION_ROLE_ARN`, `FS_ID`, etc.), then register it.

```cmd
aws ecs register-task-definition ^
  --cli-input-json file://app-task-def.json ^
  --region %AWS_REGION%
```

---

## 13. Application Load Balancer

```cmd
FOR /F "tokens=*" %i IN ('aws elbv2 create-load-balancer --name doc-parser-alb --subnets %SUBNET1% %SUBNET2% --security-groups %ALB_SG% --scheme internet-facing --type application --query "LoadBalancers[0].LoadBalancerArn" --output text') DO set ALB_ARN=%i

FOR /F "tokens=*" %i IN ('aws elbv2 create-target-group --name doc-parser-app-tg --protocol HTTP --port 8000 --target-type ip --vpc-id %VPC_ID% --health-check-path /health --query "TargetGroups[0].TargetGroupArn" --output text') DO set APP_TG_ARN=%i

FOR /F "tokens=*" %i IN ('aws elbv2 create-listener --load-balancer-arn %ALB_ARN% --protocol HTTP --port 80 --default-actions Type=forward,TargetGroupArn=%APP_TG_ARN% --query "Listeners[0].ListenerArn" --output text') DO set LISTENER_ARN=%i

aws elbv2 modify-load-balancer-attributes ^
  --load-balancer-arn %ALB_ARN% ^
  --attributes Key=idle_timeout.timeout_seconds,Value=300 ^
  --region %AWS_REGION%

FOR /F "tokens=*" %i IN ('aws elbv2 describe-load-balancers --load-balancer-arns %ALB_ARN% --query "LoadBalancers[0].DNSName" --output text') DO set ALB_DNS=%i
echo Public URL: http://%ALB_DNS%
```

---

## 14. ECS Services

```cmd
aws ecs create-service ^
  --cluster %CLUSTER_NAME% ^
  --service-name doc-parser-app ^
  --task-definition doc-parser-app ^
  --desired-count 1 ^
  --launch-type FARGATE ^
  --enable-execute-command ^
  --network-configuration "awsvpcConfiguration={subnets=[%SUBNET1%,%SUBNET2%],securityGroups=[%ECS_SG%],assignPublicIp=ENABLED}" ^
  --load-balancers "targetGroupArn=%APP_TG_ARN%,containerName=app,containerPort=8000" ^
  --region %AWS_REGION%

aws ecs wait services-stable ^
  --cluster %CLUSTER_NAME% ^
  --services doc-parser-app
echo Service is stable.
```

---

## 15. Ollama Model Bootstrap

This step is run **once** after the first deployment to download the model to EFS.

```cmd
FOR /F "tokens=*" %i IN ('aws ecs list-tasks --cluster %CLUSTER_NAME% --service-name doc-parser-app --query "taskArns[0]" --output text') DO set TASK_ARN=%i

aws ecs execute-command ^
  --cluster %CLUSTER_NAME% ^
  --task %TASK_ARN% ^
  --container ollama ^
  --interactive ^
  --command "ollama pull glm4v:9b"
```

---

## 16. GitHub Actions Secrets

Set these in your GitHub repository:

```cmd
gh secret set AWS_ACCESS_KEY_ID     --body "<cicd-access-key-id>"
gh secret set AWS_SECRET_ACCESS_KEY --body "<cicd-secret-access-key>"
gh secret set AWS_REGION            --body %AWS_REGION%
gh secret set ECR_REGISTRY          --body %ECR_REGISTRY%
gh secret set ECS_CLUSTER           --body %CLUSTER_NAME%
gh secret set ECS_SERVICE_APP       --body "doc-parser-app"
```

---

## 17. Verify Deployment

```cmd
aws ecs describe-services ^
  --cluster %CLUSTER_NAME% ^
  --services doc-parser-app ^
  --query "services[*].{name:serviceName,running:runningCount,desired:desiredCount,status:status}" ^
  --output table

curl http://%ALB_DNS%/health

aws logs tail /ecs/doc-parser-app --follow
```

---

## 18. Troubleshooting Common Deployment Issues

### A — Task fails to start: `AccessDeniedException` on Secrets Manager
**Symptom:** Service shows `runningCount: 0`. 
**Fix:** Re-attach the inline policy to the execution role. ECS retries automatically.

### A.2 — Task fails to start: `ResourceInitializationError` on Secrets Manager
**Cause:** The secret is not formatted as valid JSON.
**Fix:** 
```cmd
echo {"OPENAI_API_KEY":"sk-...YOUR-KEY-HERE..."} > secret.json
aws secretsmanager put-secret-value --secret-id doc-parser/openai-api-key --secret-string file://secret.json --region %AWS_REGION%
aws ecs update-service --cluster %CLUSTER_NAME% --service doc-parser-app --force-new-deployment --region %AWS_REGION%
del secret.json
```

### B — ALB health checks timing out: `Target.Timeout`
**Symptom:** Task is RUNNING but ALB target stays unhealthy.
**Fix:** Confirm port 8000 is open in the ECS Security Group.
```cmd
aws ec2 authorize-security-group-ingress ^
  --group-id %ECS_SG% ^
  --protocol tcp --port 8000 ^
  --source-group %ALB_SG% ^
  --region us-east-1
```

### C — Qdrant NFS warning on EFS (not a fatal error)
This is **expected and harmless**.

---

## 19. CI/CD Flow Reference

```
Push to any branch / PR opened
        │
        ▼
CI workflow (ci.yml)
  ├── ruff check src/ tests/ scripts/
  ├── mypy src/
  └── pytest tests/unit/ -v   (no API keys, ~30s)

Push to main (after PR merge)
        │
        ▼
CD workflow (cd.yml)
  ├── docker build + push app image  → ECR :<git-sha> + :latest
  ├── ecs update-service (app)       → force new deployment
  └── ecs wait services-stable
```

---

## 20. Rollback Procedure

```cmd
aws ecs list-task-definitions ^
  --family-prefix doc-parser-app ^
  --sort DESC ^
  --query "taskDefinitionArns[:5]" ^
  --output table

:: Roll back to a specific revision (e.g., 7)
aws ecs update-service ^
  --cluster %CLUSTER_NAME% ^
  --service doc-parser-app ^
  --task-definition doc-parser-app:7 ^
  --region %AWS_REGION%

aws ecs wait services-stable --cluster %CLUSTER_NAME% --services doc-parser-app --region %AWS_REGION%
```

---

## 21. Cost Overview — What This Infrastructure Charges Per Month

> **Read this before leaving the infrastructure running overnight or over a weekend.**

| Service | How it charges | ~Monthly cost |
|---------|---------------|--------------|
| **ECS Fargate — 2 vCPU** | $0.04048 per vCPU-hour × 2 × 730 h | ~$59 |
| **ECS Fargate — 16 GB RAM** | $0.004445 per GB-hour × 16 × 730 h | ~$52 |
| **Application Load Balancer** | $0.0225/hour fixed | ~$16 |
| **EFS storage (~10 GB)** | $0.30 per GB-month | ~$3 |
| **CloudWatch Logs** | $0.50 per GB ingested (~2 GB/month) | ~$1 |
| **Secrets Manager** | $0.40 per secret per month | ~$0.40 |
| **ECR storage (~2 GB)** | $0.10 per GB-month | ~$0.20 |
| **Total** | | **~$131/month** |

---

## 22. How to Stop the Infrastructure (Save Money, Keep Data)

```cmd
:: Scale ECS to 0 tasks (Stops Fargate charges)
aws ecs update-service ^
  --cluster %CLUSTER_NAME% ^
  --service doc-parser-app ^
  --desired-count 0 ^
  --region us-east-1

:: Delete ALB and Listener (Stops ALB hourly charge)
FOR /F "tokens=*" %i IN ('aws elbv2 describe-load-balancers --names doc-parser-alb --query "LoadBalancers[0].LoadBalancerArn" --output text') DO set ALB_ARN=%i

FOR /F "tokens=*" %i IN ('aws elbv2 describe-listeners --load-balancer-arn %ALB_ARN% --query "Listeners[0].ListenerArn" --output text') DO set LISTENER_ARN=%i

aws elbv2 delete-listener --listener-arn %LISTENER_ARN% --region us-east-1
aws elbv2 delete-load-balancer --load-balancer-arn %ALB_ARN% --region us-east-1
```

---

## 23. How to Restart the Infrastructure

```cmd
:: Recreate ALB
FOR /F "tokens=*" %i IN ('aws elbv2 create-load-balancer --name doc-parser-alb --subnets %SUBNET1% %SUBNET2% --security-groups %ALB_SG% --region us-east-1 --query "LoadBalancers[0].LoadBalancerArn" --output text') DO set ALB_ARN=%i

aws elbv2 modify-load-balancer-attributes ^
  --load-balancer-arn %ALB_ARN% ^
  --attributes Key=idle_timeout.timeout_seconds,Value=300 ^
  --region us-east-1

:: Recreate Listener
FOR /F "tokens=*" %i IN ('aws elbv2 describe-target-groups --names doc-parser-app-tg --query "TargetGroups[0].TargetGroupArn" --output text') DO set TG_ARN=%i

aws elbv2 create-listener ^
  --load-balancer-arn %ALB_ARN% ^
  --protocol HTTP --port 80 ^
  --default-actions Type=forward,TargetGroupArn=%TG_ARN% ^
  --region us-east-1

:: Scale ECS back up
aws ecs update-service --cluster doc-parser-cluster --service doc-parser-app --desired-count 1 --region us-east-1
```

---

## 24. How to Tear Down Everything (Full Deletion)

> **Warning — this is irreversible.**

```cmd
aws ecs update-service --cluster %CLUSTER_NAME% --service doc-parser-app --desired-count 0 --region us-east-1
aws ecs wait services-stable --cluster %CLUSTER_NAME% --services doc-parser-app --region us-east-1
aws ecs delete-service --cluster %CLUSTER_NAME% --service doc-parser-app --region us-east-1

FOR /F "tokens=*" %i IN ('aws elbv2 describe-load-balancers --names doc-parser-alb --query "LoadBalancers[0].LoadBalancerArn" --output text') DO set ALB_ARN=%i
FOR /F "tokens=*" %i IN ('aws elbv2 describe-listeners --load-balancer-arn %ALB_ARN% --query "Listeners[0].ListenerArn" --output text') DO set LISTENER_ARN=%i
aws elbv2 delete-listener --listener-arn %LISTENER_ARN% --region us-east-1
aws elbv2 delete-load-balancer --load-balancer-arn %ALB_ARN% --region us-east-1

FOR /F "tokens=*" %i IN ('aws elbv2 describe-target-groups --names doc-parser-app-tg --query "TargetGroups[0].TargetGroupArn" --output text') DO set TG_ARN=%i
aws elbv2 delete-target-group --target-group-arn %TG_ARN% --region us-east-1

:: Note: Manually delete EFS Access Points and Mount Targets via the AWS Console, then run:
aws efs delete-file-system --file-system-id %FS_ID% --region us-east-1

aws ecs delete-cluster --cluster %CLUSTER_NAME% --region us-east-1
aws ecr delete-repository --repository-name doc-parser/app --force --region us-east-1
```
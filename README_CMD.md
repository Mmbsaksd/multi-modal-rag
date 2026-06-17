# Step-by-Step AWS Deployment Guide (Windows Command Prompt)

**Account:** `685057748560` | **Region:** `us-east-1` | **Parser:** Ollama

This guide is the single source of truth for deploying the MultiModal RAG pipeline to AWS using **Windows CMD**.

---

## 1. Prerequisites
Install Docker, AWS CLI v2, jq, and GitHub CLI, then run:
```cmd
gh auth login
```

## 2. IAM & Shell Variables
Create an Admin user via the AWS console, create an access key, and run:
```cmd
aws configure --profile doc-parser-admin
set AWS_PROFILE=doc-parser-admin
set AWS_REGION=us-east-1
set CLUSTER_NAME=doc-parser-cluster

FOR /F "tokens=*" %i IN ('aws sts get-caller-identity --query "Account" --output text') DO set AWS_ACCOUNT_ID=%i
set ECR_REGISTRY=%AWS_ACCOUNT_ID%.dkr.ecr.%AWS_REGION%.amazonaws.com

FOR /F "tokens=*" %i IN ('aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" --query "Vpcs[0].VpcId" --output text') DO set VPC_ID=%i
```

Find your Subnets and manually set them (replace IDs below):
```cmd
aws ec2 describe-subnets --filters "Name=defaultForAz,Values=true" --query "Subnets[*].SubnetId" --output text
set SUBNET1=subnet-XXXXXXXX
set SUBNET2=subnet-YYYYYYYY
```

## 3. Security Groups
```cmd
FOR /F "tokens=*" %i IN ('aws ec2 create-security-group --group-name doc-parser-alb-sg --description "ALB" --vpc-id %VPC_ID% --query "GroupId" --output text') DO set ALB_SG=%i
aws ec2 authorize-security-group-ingress --group-id %ALB_SG% --protocol tcp --port 80 --cidr 0.0.0.0/0

FOR /F "tokens=*" %i IN ('aws ec2 create-security-group --group-name doc-parser-ecs-sg --description "ECS" --vpc-id %VPC_ID% --query "GroupId" --output text') DO set ECS_SG=%i
aws ec2 authorize-security-group-ingress --group-id %ECS_SG% --protocol tcp --port 8000 --source-group %ALB_SG%
aws ec2 authorize-security-group-ingress --group-id %ECS_SG% --protocol tcp --port 2049 --source-group %ECS_SG%
```

## 4. ECR and ECS Cluster
```cmd
aws ecr create-repository --repository-name doc-parser/app --region %AWS_REGION%
aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com
aws ecs create-cluster --cluster-name %CLUSTER_NAME% --capacity-providers FARGATE FARGATE_SPOT --region %AWS_REGION%
```

## 5. EFS — Persistent Storage
```cmd
FOR /F "tokens=*" %i IN ('aws efs create-file-system --performance-mode generalPurpose --region %AWS_REGION% --query "FileSystemId" --output text') DO set FS_ID=%i

aws efs create-mount-target --file-system-id %FS_ID% --subnet-id %SUBNET1% --security-groups %ECS_SG%
aws efs create-mount-target --file-system-id %FS_ID% --subnet-id %SUBNET2% --security-groups %ECS_SG%

FOR /F "tokens=*" %i IN ('aws efs create-access-point --file-system-id %FS_ID% --posix-user Uid=1000,Gid=1000 --root-directory "Path=/qdrant,CreationInfo={OwnerUid=1000,OwnerGid=1000,Permissions=755}" --query "AccessPointId" --output text') DO set QDRANT_AP=%i
FOR /F "tokens=*" %i IN ('aws efs create-access-point --file-system-id %FS_ID% --posix-user Uid=0,Gid=0 --root-directory "Path=/ollama,CreationInfo={OwnerUid=0,OwnerGid=0,Permissions=755}" --query "AccessPointId" --output text') DO set OLLAMA_AP=%i
```

## 6. Secrets Manager
To avoid CMD quote-stripping issues, use a file to upload the secret:
```cmd
echo {"OPENAI_API_KEY":"sk-YOUR-KEY-HERE"} > secret.json
aws secretsmanager create-secret --name doc-parser/openai-api-key --secret-string file://secret.json --region %AWS_REGION%
del secret.json
```

## 7. IAM Roles & Policies
Create files `cicd-policy.json`, `trust-policy.json`, and `exec-policy.json` (as described in your repository docs) and run:
```cmd
aws iam create-user --user-name doc-parser-cicd
aws iam create-access-key --user-name doc-parser-cicd
aws iam put-user-policy --user-name doc-parser-cicd --policy-name doc-parser-cicd-policy --policy-document file://cicd-policy.json

aws iam create-role --role-name doc-parser-ecs-task-execution --assume-role-policy-document file://trust-policy.json
aws iam attach-role-policy --role-name doc-parser-ecs-task-execution --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name doc-parser-ecs-task-execution --policy-name custom-exec --policy-document file://exec-policy.json

set EXECUTION_ROLE_ARN=arn:aws:iam::%AWS_ACCOUNT_ID%:role/doc-parser-ecs-task-execution
```

## 8. CloudWatch Log Groups & Task Definitions
```cmd
aws logs create-log-group --log-group-name /ecs/doc-parser-app --region %AWS_REGION%
```
Open `app-task-def.json` in Notepad. Replace placeholders with variables you generated above, then run:
```cmd
aws ecs register-task-definition --cli-input-json file://app-task-def.json --region %AWS_REGION%
```

## 9. Application Load Balancer
```cmd
FOR /F "tokens=*" %i IN ('aws elbv2 create-load-balancer --name doc-parser-alb --subnets %SUBNET1% %SUBNET2% --security-groups %ALB_SG% --scheme internet-facing --type application --query "LoadBalancers[0].LoadBalancerArn" --output text') DO set ALB_ARN=%i

FOR /F "tokens=*" %i IN ('aws elbv2 create-target-group --name doc-parser-app-tg --protocol HTTP --port 8000 --target-type ip --vpc-id %VPC_ID% --health-check-path /health --query "TargetGroups[0].TargetGroupArn" --output text') DO set APP_TG_ARN=%i

FOR /F "tokens=*" %i IN ('aws elbv2 create-listener --load-balancer-arn %ALB_ARN% --protocol HTTP --port 80 --default-actions Type=forward,TargetGroupArn=%APP_TG_ARN% --query "Listeners[0].ListenerArn" --output text') DO set LISTENER_ARN=%i

aws elbv2 modify-load-balancer-attributes --load-balancer-arn %ALB_ARN% --attributes Key=idle_timeout.timeout_seconds,Value=300 --region %AWS_REGION%

FOR /F "tokens=*" %i IN ('aws elbv2 describe-load-balancers --load-balancer-arns %ALB_ARN% --query "LoadBalancers[0].DNSName" --output text') DO set ALB_DNS=%i
echo Public URL: http://%ALB_DNS%
```

## 10. ECS Services
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

aws ecs wait services-stable --cluster %CLUSTER_NAME% --services doc-parser-app
```

## 11. Ollama Model Bootstrap
```cmd
FOR /F "tokens=*" %i IN ('aws ecs list-tasks --cluster %CLUSTER_NAME% --service-name doc-parser-app --query "taskArns[0]" --output text') DO set TASK_ARN=%i

aws ecs execute-command ^
  --cluster %CLUSTER_NAME% ^
  --task %TASK_ARN% ^
  --container ollama ^
  --interactive ^
  --command "ollama pull glm4v:9b"
```

## 12. GitHub Actions Secrets
Run these commands replacing the placeholders:
```cmd
gh secret set AWS_ACCESS_KEY_ID     --body "CICD_ACCESS_KEY"
gh secret set AWS_SECRET_ACCESS_KEY --body "CICD_SECRET_KEY"
gh secret set AWS_REGION            --body %AWS_REGION%
gh secret set ECR_REGISTRY          --body %ECR_REGISTRY%
gh secret set ECS_CLUSTER           --body %CLUSTER_NAME%
gh secret set ECS_SERVICE_APP       --body "doc-parser-app"
```

---

## 13. Verify Deployment
```cmd
aws ecs describe-services --cluster %CLUSTER_NAME% --services doc-parser-app --query "services[*].{name:serviceName,running:runningCount,desired:desiredCount,status:status}" --output table
curl http://%ALB_DNS%/health
```

## 14. Troubleshooting
**Task fails: `ResourceInitializationError` on Secrets Manager**
```cmd
echo {"OPENAI_API_KEY":"sk-YOUR-KEY"} > secret.json
aws secretsmanager put-secret-value --secret-id doc-parser/openai-api-key --secret-string file://secret.json --region %AWS_REGION%
aws ecs update-service --cluster %CLUSTER_NAME% --service doc-parser-app --force-new-deployment --region %AWS_REGION%
del secret.json
```
**ALB Health Checks Timing Out (`Target.Timeout`)**
```cmd
aws ec2 authorize-security-group-ingress --group-id %ECS_SG% --protocol tcp --port 8000 --source-group %ALB_SG%
```

## 15. Rollback Procedure
```cmd
aws ecs update-service --cluster %CLUSTER_NAME% --service doc-parser-app --task-definition doc-parser-app:1 --region %AWS_REGION%
aws ecs wait services-stable --cluster %CLUSTER_NAME% --services doc-parser-app
```

## 16. Cost Overview
Fixed costs are ~$131/month (Fargate + ALB). Variable costs depend on API usage. **Stop your infrastructure when not in use!**

## 17. Stop Infrastructure (Save Money)
```cmd
aws ecs update-service --cluster %CLUSTER_NAME% --service doc-parser-app --desired-count 0 --region %AWS_REGION%
aws elbv2 delete-listener --listener-arn %LISTENER_ARN% --region %AWS_REGION%
aws elbv2 delete-load-balancer --load-balancer-arn %ALB_ARN% --region %AWS_REGION%
```

## 18. Restart Infrastructure
```cmd
FOR /F "tokens=*" %i IN ('aws elbv2 create-load-balancer --name doc-parser-alb --subnets %SUBNET1% %SUBNET2% --security-groups %ALB_SG% --scheme internet-facing --type application --query "LoadBalancers[0].LoadBalancerArn" --output text') DO set ALB_ARN=%i
aws elbv2 modify-load-balancer-attributes --load-balancer-arn %ALB_ARN% --attributes Key=idle_timeout.timeout_seconds,Value=300 --region %AWS_REGION%
FOR /F "tokens=*" %i IN ('aws elbv2 create-listener --load-balancer-arn %ALB_ARN% --protocol HTTP --port 80 --default-actions Type=forward,TargetGroupArn=%APP_TG_ARN% --query "Listeners[0].ListenerArn" --output text') DO set LISTENER_ARN=%i
aws ecs update-service --cluster %CLUSTER_NAME% --service doc-parser-app --desired-count 1 --region %AWS_REGION%
```

## 19. Full Teardown (Irreversible)
```cmd
aws ecs delete-service --cluster %CLUSTER_NAME% --service doc-parser-app --force --region %AWS_REGION%

:: You will need to manually delete EFS Access Points and Mount Targets via the AWS Console 
:: or write a short script to loop over them. Then delete the file system:
:: aws efs delete-file-system --file-system-id %FS_ID% --region %AWS_REGION%

aws ecs delete-cluster --cluster %CLUSTER_NAME% --region %AWS_REGION%
aws ecr delete-repository --repository-name doc-parser/app --force --region %AWS_REGION%
```